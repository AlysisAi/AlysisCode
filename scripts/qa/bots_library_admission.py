"""Bounded PostgreSQL/Azurite account-Library contention; no model or cloud claim.

Requires a disposable database DSN in ALYSIS_BOTS_TEST_POSTGRES_DSN. Synthetic
keys enter the emulator through environment only. Existing services are untouched.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from azure.storage.blob import ContainerClient

from alysis_code.bots.models import BotInput
from alysis_code.bots.object_storage_azure import AzureBlobStorage
from alysis_code.bots.postgres import PostgresStore
from alysis_code.bots.store import LimitReached, NotFound
from alysis_code.bots.transfers import FileTransfers, UploadChunk, UploadInput


def run(output, *, port=7194, accounts=12, bots_per_account=4):
    if not 2 <= accounts <= 20 or not 2 <= bots_per_account <= 10:
        raise ValueError("Use a bounded synthetic study")
    # Refuse to displace another local service.
    with socket.socket() as check:
        check.bind(("127.0.0.1", port))
    output.mkdir(parents=True, exist_ok=False)
    name = "bots-library-admission-" + uuid4().hex[:10]
    account = "librarytest"
    key = base64.b64encode(os.urandom(32)).decode()
    url = f"http://127.0.0.1:{port}/{account}"
    receipt = {
        "kind": "Actual local PostgreSQL/Azurite concurrency; synthetic file data; no models or Azure",
        "passed": False,
        "container": name,
        "accounts": accounts,
        "bots_per_account": bots_per_account,
        "checks": {},
    }
    objects = store = container = None
    launched = False
    try:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--detach",
                "--rm",
                "--name",
                name,
                "--memory",
                "512m",
                "--cpus",
                "1",
                "--publish",
                f"127.0.0.1:{port}:10000",
                "--env",
                "AZURITE_ACCOUNTS",
                "mcr.microsoft.com/azure-storage/azurite:3.35.0",
                "azurite-blob",
                "--blobHost",
                "0.0.0.0",
                "--silent",
                "--skipApiVersionCheck",
            ],
            env={**os.environ, "AZURITE_ACCOUNTS": account + ":" + key},
            capture_output=True,
            timeout=60,
        )
        if result.returncode:
            raise RuntimeError("Isolated emulator did not start")
        launched = True
        container = ContainerClient(url, "study", credential=key, retry_total=0)
        deadline = time.monotonic() + 20
        while True:
            try:
                container.create_container()
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise RuntimeError("Emulator startup deadline") from None
                time.sleep(0.2)
        objects = AzureBlobStorage(url, "study", key, emulator=True)
        store = PostgresStore(
            os.environ["ALYSIS_BOTS_TEST_POSTGRES_DSN"],
            objects=objects,
            state_directory=output / "controller",
        )
        owners = ["library-study-" + uuid4().hex for _ in range(accounts)]
        bots = {}
        allowance = 131071
        for owner in owners:
            store.set_library_limit(owner, allowance)
            bots[owner] = [
                store.save_bot(
                    owner, BotInput(name=f"Bot {i}", purpose="Synthetic Library admission")
                )["id"]
                for i in range(bots_per_account)
            ]
        # Interleave owners and bots; real network staging happens outside SQL
        # transactions while competing uploads hold durable byte reservations.
        jobs = [
            (owner, bot, index) for index in range(4) for owner in owners for bot in bots[owner]
        ]

        def write(job):
            owner, bot, index = job
            payload = os.urandom(16384 + secrets.randbelow(32768))
            started = time.monotonic()
            try:
                file = store.save_bytes(owner, bot, f"東京-{index}.bin", payload)
                assert store.file_data(owner, bot, file["id"]) == payload
                return dict(
                    owner=owner,
                    bot=bot,
                    file=file["id"],
                    bytes=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                    accepted=True,
                    seconds=time.monotonic() - started,
                )
            except LimitReached:
                return dict(
                    owner=owner, bot=bot, accepted=False, seconds=time.monotonic() - started
                )

        with ThreadPoolExecutor(max_workers=12) as pool:
            outcomes = list(pool.map(write, jobs))
        accepted = [r for r in outcomes if r["accepted"]]
        rejected = [r for r in outcomes if not r["accepted"]]
        assert accepted and rejected
        for owner in owners:
            usage = store.library_usage(owner)
            assert usage["used_bytes"] == sum(r["bytes"] for r in accepted if r["owner"] == owner)
            assert usage["used_bytes"] <= allowance and usage["reserved_bytes"] == 0
            assert isinstance(usage["available_bytes"], int)
        receipt["checks"]["concurrent_account_admission"] = True
        # New controller, same persisted SQL/object state, no copied cache.
        store.close()
        store = PostgresStore(
            os.environ["ALYSIS_BOTS_TEST_POSTGRES_DSN"],
            objects=objects,
            state_directory=output / "replacement",
        )
        for owner in owners:
            files = [r for r in accepted if r["owner"] == owner]
            before = store.library_usage(owner)
            store.set_library_limit(owner, 0)
            for row in files:
                assert (
                    hashlib.sha256(store.file_data(owner, row["bot"], row["file"])).hexdigest()
                    == row["sha256"]
                )
            assert store.library_usage(owner)["used_bytes"] == before["used_bytes"]
            first = files[0]
            with_denied = False
            try:
                store.file_data(
                    owners[(owners.index(owner) + 1) % len(owners)], first["bot"], first["file"]
                )
            except NotFound:
                with_denied = True
            assert with_denied
            store.delete_item(owner, first["bot"], first["file"], kind="files")
            after = store.library_usage(owner)
            assert after["used_bytes"] == before["used_bytes"] - first["bytes"]
            # Small exact headroom shared across all this owner's bots.
            store.set_library_limit(owner, after["used_bytes"] + 1024)
            transfer = FileTransfers(store, owner)
            upload = transfer.begin(
                bots[owner][-1], UploadInput(name="reserved.bin", size=1024, request_id=uuid4().hex)
            )
            assert store.library_usage(owner)["reserved_bytes"] == 1024
            try:
                transfer.begin(
                    bots[owner][0], UploadInput(name="blocked.bin", size=1, request_id=uuid4().hex)
                )
                raise AssertionError("Concurrent reservation over-admitted")
            except LimitReached:
                pass
            transfer.cancel(bots[owner][-1], upload["id"])
            assert store.library_usage(owner)["reserved_bytes"] == 0
            upload = transfer.begin(
                bots[owner][0], UploadInput(name="completed.bin", size=1024, request_id=uuid4().hex)
            )
            data = os.urandom(1024)
            transfer.append(
                bots[owner][0],
                upload["id"],
                UploadChunk(offset=0, data_base64=base64.b64encode(data).decode()),
            )
            final = transfer.complete(bots[owner][0], upload["id"])
            assert store.file_data(owner, bots[owner][0], final["file_id"]) == data
            assert store.library_usage(owner)["used_bytes"] == after["used_bytes"] + 1024
            assert store.library_usage(owner)["reserved_bytes"] == 0
        receipt["checks"].update(
            restart_hashes=True,
            lowering_preserves_files=True,
            owner_isolation=True,
            deletion_releases_logical_space=True,
            cancellation_and_transfer_accounting=True,
        )
        timings = sorted(r["seconds"] for r in outcomes)
        receipt.update(
            accepted=len(accepted),
            quota_denied=len(rejected),
            attempts=len(outcomes),
            p50_seconds=timings[len(timings) // 2],
            p95_seconds=timings[int((len(timings) - 1) * 0.95)],
            passed=True,
        )
    except Exception as error:
        receipt["failure_type"] = type(error).__name__
        raise
    finally:
        if store:
            store.close()
        if objects:
            objects.close()
        if container:
            container.close()
        if launched:
            result = subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
            receipt["emulator_removed"] = result.returncode == 0
        (output / "receipt.json").write_text(json.dumps(receipt, indent=2))
        print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=7194)
    args = parser.parse_args()
    run(args.output.resolve(), port=args.port)
