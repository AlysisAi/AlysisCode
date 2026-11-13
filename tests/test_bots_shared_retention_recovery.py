"""Actual PostgreSQL/Azurite deletion recovery across controller SIGKILL.

Object bytes and controller processes are real; a diagnostic pause follows the
actual delete. Eligibility uses an explicitly advanced clock with the supported
five-minute minimum policy, not a claim that production retention time elapsed.
No model, task-computer, Azure account or public ingress is used.
"""

import base64
import hashlib
import io
import json
import os
import selectors
import signal
import subprocess
import sys
import tarfile
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from test_bots_backup import databases  # noqa: F401

from alysis_code.bots.models import BotInput, FileInput, TaskInput
from alysis_code.bots.object_storage import ObjectRef, ObjectStorageError
from alysis_code.bots.object_storage_azure import AzureBlobStorage
from alysis_code.bots.postgres import PostgresStore
from alysis_code.bots.retention import ObjectRetention, RetentionPolicy
from alysis_code.bots.store import Conflict
from alysis_code.bots.workspace_objects import WorkspaceObjects

POLICY = RetentionPolicy(300, 300, 2, 300)


def docker(*args, env=None):
    result = subprocess.run(
        ["docker", "--context", "desktop-linux", *args],
        env=env,
        capture_output=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("Isolated retention storage operation failed.")
    return result.stdout


@contextmanager
def storage_service():
    from azure.storage.blob import ContainerClient

    name = "alysis-bots-retention-" + uuid4().hex[:12]
    config = {
        "url": "http://127.0.0.1:7094/retentionstudy",
        "container": "synthetic",
        "key": base64.b64encode(os.urandom(32)).decode(),
    }
    objects = client = None
    started = False
    try:
        docker(
            "run",
            "--rm",
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
            env={**os.environ, "AZURITE_ACCOUNTS": "retentionstudy:" + config["key"]},
        )
        started = True
        client = ContainerClient(
            config["url"],
            config["container"],
            credential=config["key"],
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
                    raise RuntimeError("Retention emulator did not become ready.") from None
                time.sleep(0.2)
        objects = AzureBlobStorage(config["url"], config["container"], config["key"], emulator=True)
        yield objects, config, name
    finally:
        if objects:
            objects.close()
        if client:
            client.close()
        if started:
            docker("stop", "--time", "5", name)
            # Docker's --rm removal can complete just after stop returns.
            deadline = time.monotonic() + 10
            while name in docker("ps", "--all", "--format", "{{.Names}}").decode().splitlines():
                if time.monotonic() >= deadline:
                    raise RuntimeError("Retention emulator removal was not confirmed.")
                time.sleep(0.1)


def child(config):
    class InterruptedDelete(AzureBlobStorage):
        def delete(self, ref):
            super().delete(ref)
            print(json.dumps({"deleted_key": ref.key}), flush=True)
            # The storage service has confirmed deletion. Keep the database
            # acknowledgement outstanding until the parent kills this process.
            while True:
                time.sleep(1)

    storage = config["storage"]
    objects = InterruptedDelete(storage["url"], storage["container"], storage["key"], emulator=True)
    store = PostgresStore(config["dsn"], objects=objects, state_directory=Path(config["scratch"]))
    ObjectRetention(store, config["owner"], POLICY).sweep(now=config["now"], limit=1)
    raise AssertionError("Interrupted deletion unexpectedly returned.")


def archive():
    stream = io.BytesIO()
    data = os.urandom(513)
    with tarfile.open(fileobj=stream, mode="w") as output:
        entry = tarfile.TarInfo("archive/東京.bin")
        entry.size = len(data)
        output.addfile(entry, io.BytesIO(data))
    return stream.getvalue()


def test_controller_death_after_actual_blob_delete_preserves_files_and_resumes_cleanup(
    databases,  # noqa: F811
    tmp_path,
):
    if os.environ.get("ALYSIS_BOTS_TEST_DOCKER") != "1":
        pytest.skip("Enable the isolated real storage service explicitly")
    process = replacement = None
    storage_name = None
    receipt = {
        "passed": False,
        "kind": "Actual local PostgreSQL/Azurite/controller SIGKILL; explicit deletion pause",
        "eligibility_clock": "Advanced test clock, 300-second minimum policy",
        "external_model_calls": 0,
        "azure_calls": 0,
        "task_computers": 0,
    }
    try:
        with storage_service() as (objects, config, storage_name):
            store = databases(objects=objects)
            owner, other = "cleanup-" + uuid4().hex, "protected-" + uuid4().hex
            bot = store.save_bot(owner, BotInput(name="Αθήνα", purpose="Retention recovery"))["id"]
            peer = store.save_bot(other, BotInput(name="東京", purpose="Separate account"))["id"]
            active_bytes, other_bytes = os.urandom(4097), os.urandom(2053)
            active = store.save_bytes(owner, bot, "保存.bin", active_bytes)
            peer_file = store.save_bytes(other, peer, "保存.bin", other_bytes)
            removed = store.save_bytes(owner, bot, "removed.bin", os.urandom(217))
            with store.connect() as db:
                removed_ref = ObjectRef.decode(
                    db.execute(
                        "SELECT ref FROM file_objects WHERE file_id=?", (removed["id"],)
                    ).fetchone()[0]
                )
            store.delete_item(owner, bot, removed["id"], kind="files")
            pending_input = FileInput(name="late.txt", content="Μετά την αποσύνδεση 東京")
            pending_bytes = pending_input.content.encode()
            pending = store.stage_file_object(owner, bot, pending_input, None, None)
            next_orphan_bytes = os.urandom(19)
            next_orphan = store.put_object(owner, bot, [next_orphan_bytes], len(next_orphan_bytes))
            other_orphan_bytes, unknown_bytes = os.urandom(31), os.urandom(47)
            other_orphan = store.put_object(
                other, peer, [other_orphan_bytes], len(other_orphan_bytes)
            )
            # An unregistered object is deliberately outside cleaner authority.
            unknown = objects.put(owner, bot, [unknown_bytes], len(unknown_bytes))
            checkpoints = []
            chat = store.create_chat(owner, bot, "Continuous")["id"]
            for index in range(4):
                queued = store.enqueue(
                    owner, chat, TaskInput(message="Synthetic checkpoint", request_id=uuid4().hex)
                )
                assert store.claim(owner)["id"] == queued["id"]
                body = archive()
                ref = WorkspaceObjects(store).stage(owner, bot, queued["id"], [body], len(body))
                checkpoints.append((ref, body))
                if index < 3:
                    store.finish(owner, queued["id"], "completed", content="Checkpoint fixture")
            now = time.time() + 301

            def state(ref):
                with store.connect() as db:
                    row = db.execute(
                        "SELECT state FROM stored_objects WHERE key=?", (ref.key,)
                    ).fetchone()
                    return row[0] if row else None

            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--worker"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            process.stdin.write(
                json.dumps(
                    {
                        "dsn": store._dsn,
                        "storage": config,
                        "owner": owner,
                        "scratch": str(tmp_path / "doomed-cleaner"),
                        "now": now,
                    }
                )
                + "\n"
            )
            process.stdin.close()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                assert selector.select(30), "Cleaner did not confirm its actual storage deletion."
                line = process.stdout.readline()
            assert line and json.loads(line) == {"deleted_key": pending.key}
            assert state(pending) == "deleting"
            with pytest.raises(ObjectStorageError):
                list(objects.read(pending))
            with pytest.raises(Conflict, match="Object upload expired"):
                store._save_file(owner, bot, pending_input, object_ref=pending)
            # The stalled network boundary holds no database transaction.
            start = time.monotonic()
            concurrent_bytes = os.urandom(97)
            concurrent = store.save_bytes(other, peer, "during-cleanup.bin", concurrent_bytes)
            independent_write_seconds = time.monotonic() - start
            assert store.file(other, peer, concurrent["id"])["sha256"] == concurrent["sha256"]

            process.kill()
            assert process.wait(timeout=10) == -signal.SIGKILL
            assert state(pending) == "deleting"
            replacement = PostgresStore(
                store._dsn, objects=objects, state_directory=tmp_path / "fresh-cleaner"
            )
            result = ObjectRetention(replacement, owner, POLICY).sweep(now=now, limit=1)
            assert result == {"deleted": 1, "pending": 0} and state(next_orphan) == "deleted"
            assert state(pending) == "deleting"
            with pytest.raises(ObjectStorageError):
                list(objects.read(next_orphan))
            # The interrupted attempt moved behind untouched work. Both can
            # progress immediately after restart without a retention-clock jump.
            result = ObjectRetention(replacement, owner, POLICY).sweep(now=now, limit=1)
            assert result == {"deleted": 1, "pending": 0} and state(pending) == "deleted"
            # Repeat from persisted state: no additional deletion is due yet.
            assert ObjectRetention(replacement, owner, POLICY).sweep(now=now) == {
                "deleted": 0,
                "pending": 0,
            }
            for ref, body in checkpoints:
                assert b"".join(objects.read(ref)) == body

            # Simulate an upload arriving after deletion using a real Blob PUT,
            # not a real delayed network request. It must never become a Library file.
            objects._upload(pending, io.BytesIO(pending_bytes))
            assert b"".join(objects.read(pending)) == pending_bytes
            with pytest.raises(Conflict, match="Object upload expired"):
                replacement._save_file(owner, bot, pending_input, object_ref=pending)
            result = ObjectRetention(replacement, owner, POLICY).sweep(now=now + 301)
            # Four acknowledged checks: two tombstones, the removed Library
            # object and the old checkpoint. One tombstone was already absent.
            assert result == {"deleted": 4, "pending": 0}
            for ref in (pending, removed_ref, checkpoints[0][0]):
                assert state(ref) == "deleted"
                with pytest.raises(ObjectStorageError):
                    list(objects.read(ref))
            assert replacement.file_data(owner, bot, active["id"]) == active_bytes
            assert replacement.file_data(other, peer, peer_file["id"]) == other_bytes
            assert replacement.file_data(other, peer, concurrent["id"]) == concurrent_bytes
            assert b"".join(objects.read(other_orphan)) == other_orphan_bytes
            assert b"".join(objects.read(unknown)) == unknown_bytes and state(unknown) is None
            assert state(other_orphan) == "staged"
            for ref, body in checkpoints[1:]:
                assert state(ref) == "live" and b"".join(objects.read(ref)) == body
            assert replacement.task(owner, queued["id"])["status"] == "running"
            assert [f["id"] for f in replacement.list_files(owner, bot)] == [active["id"]]
            assert len(replacement.list_files(other, peer)) == 2
            assert ObjectRetention(replacement, owner, POLICY).sweep(now=now + 301) == {
                "deleted": 0,
                "pending": 0,
            }
            receipt.update(
                passed=True,
                actual_delete_before_sigkill=True,
                durable_deleting_state_survived=True,
                restart_reconciled_missing_object=True,
                untouched_orphan_progressed_before_interrupted_retry=True,
                late_publication_rejected=True,
                late_uploaded_object_deleted=True,
                successful_checkpoints_preserved=2,
                active_checkpoint_preserved=True,
                library_files_preserved=3,
                unknown_and_other_account_orphans_preserved=True,
                other_account_write_seconds=round(independent_write_seconds, 3),
                protected_file_sha256=hashlib.sha256(active_bytes).hexdigest(),
            )
            replacement.close()
            replacement = None
    finally:
        if process and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        if replacement:
            replacement.close()
        removed = (
            storage_name is None
            or storage_name
            not in docker("ps", "--all", "--format", "{{.Names}}").decode().splitlines()
        )
        receipt.update(
            storage_container=storage_name,
            storage_container_removed=removed,
            child_process_stopped=process is None or process.poll() is not None,
        )
        receipt["passed"] = receipt["passed"] and removed and receipt["child_process_stopped"]
        output = os.environ.get("ALYSIS_BOTS_RETENTION_RECOVERY_RECEIPT")
        if output:
            Path(output).write_text(json.dumps(receipt, indent=2) + "\n")
        assert removed, "Retention study storage container was not removed."


if __name__ == "__main__":
    try:
        child(json.loads(sys.stdin.readline()))
    except Exception as error:
        raise SystemExit(type(error).__name__) from None
