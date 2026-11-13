"""Actual Linux allocation-response loss and PostgreSQL capacity recovery.

Only the transport pause is a fixture: Docker creates the computer before the
controller is killed. Recovery uses the normal 60-second account lease, a fresh
scratch directory, and real provider discovery/deletion. No model/cloud calls.
"""

import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import pytest
from test_bots_backup import databases  # noqa: F401

from alysis_code.bots.allocation_ledger import AllocationLedger
from alysis_code.bots.cloud_desktop import CloudDesktopFactory
from alysis_code.bots.compute import ComputerIdentity, ComputerRef, ComputerSpec
from alysis_code.bots.compute_docker import DockerComputers
from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.desktop import DesktopComputers
from alysis_code.bots.desktop_linux import DesktopError
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.object_storage import LocalObjectStorage
from alysis_code.bots.postgres import PostgresStore
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.worker_lease import WorkerLease, WorkerLeaseLost

ROOT = Path(__file__).resolve().parents[1]
SPEC = ComputerSpec("alysis-bots-shared-state-20261020:qa")


def provider():
    return DockerComputers(
        "desktop-linux", namespace_profile=ROOT / "scripts/cloud-dev/seccomp.json"
    )


def child(config):
    class PausedCreate(DockerComputers):
        def create(self, identity, spec):
            ref = super().create(identity, spec)
            assert self.status(ref).state == "running"
            print(json.dumps({"created": asdict(ref), "ttl": lease.ttl}), flush=True)
            # Pause after provider success but before CloudDesktop can persist
            # the resource ID or run any task command. The parent sends SIGKILL.
            while True:
                time.sleep(1)

    store = PostgresStore(
        config["dsn"],
        objects=LocalObjectStorage(Path(config["objects"])),
        state_directory=Path(config["scratch"]),
    )
    lease = WorkerLease(store, config["owner"])
    worker = lease.acquire()
    lease.start_heartbeat(lambda: os._exit(2))
    task = worker.claim(config["owner"])
    assert task["id"] == config["task"]
    factory = CloudDesktopFactory(
        config["owner"],
        PausedCreate("desktop-linux", namespace_profile=ROOT / "scripts/cloud-dev/seccomp.json"),
        SPEC,
        Path(config["scratch"]) / "ledger",
        store=worker,
        max_computers=2,
    )
    factory(config["bot"], Path(config["scratch"]), cancel=lambda: False)
    raise AssertionError("Paused allocation unexpectedly returned.")


def test_lost_create_response_keeps_shared_capacity_until_actual_reconciliation(
    databases,  # noqa: F811
    tmp_path,
):
    if os.environ.get("ALYSIS_BOTS_TEST_DOCKER") != "1":
        pytest.skip("Enable actual isolated Linux computers explicitly")
    objects = tmp_path / "objects"
    store = databases(objects=LocalObjectStorage(objects))
    compute = provider()
    owner = "crashed-" + uuid4().hex
    bot = store.save_bot(owner, BotInput(name="東京 Αθήνα", purpose="Allocation recovery"))["id"]
    chat = store.create_chat(owner, bot, "Continuous")["id"]
    queued = store.enqueue(
        owner, chat, TaskInput(message="Synthetic allocation boundary", request_id=uuid4().hex)
    )
    processes, leases, surfaces, refs = [], [], [], []
    runtime = None
    receipt = {
        "passed": False,
        "kind": "Actual local PostgreSQL, Docker Linux, SIGKILL; paused create-response fixture",
        "external_model_calls": 0,
        "azure_calls": 0,
    }

    def allocate_account(label):
        account = label + "-" + uuid4().hex
        current_bot = store.save_bot(
            account, BotInput(name=label, purpose="Independent capacity account")
        )["id"]
        lease = WorkerLease(store, account)
        worker = lease.acquire()
        leases.append(lease)
        lease.start_heartbeat(lambda: None)
        factory = CloudDesktopFactory(
            account, compute, SPEC, tmp_path / account, store=worker, max_computers=2
        )
        return account, current_bot, worker, factory

    def allocate(entry):
        surface = entry[3](entry[1], tmp_path, cancel=lambda: False)
        surfaces.append(surface)
        refs.append(surface.ref)
        return surface

    def occupied():
        with store.connect() as db:
            return db.execute(
                "SELECT count(*) FROM computer_allocations "
                "WHERE record::jsonb->>'scope'=? "
                "AND (record::jsonb->>'state') IS DISTINCT FROM 'deleted'",
                (compute.scope,),
            ).fetchone()[0]

    def source_record():
        with store.connect() as db:
            row = db.execute(
                "SELECT record FROM computer_allocations WHERE owner=? AND bot_id=?", (owner, bot)
            ).fetchone()
            return json.loads(row[0]) if row else None

    def remaining_lease():
        with store.connect() as db:
            row = db.execute("SELECT expires FROM worker_leases WHERE owner=?", (owner,)).fetchone()
            return row[0] - WorkerLease.now(db)

    try:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        processes.append(process)
        process.stdin.write(
            json.dumps(
                {
                    "dsn": store._dsn,
                    "objects": str(objects),
                    "scratch": str(tmp_path / "crashed-controller"),
                    "owner": owner,
                    "bot": bot,
                    "task": queued["id"],
                }
            )
            + "\n"
        )
        process.stdin.close()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(30), "Allocation child did not return its provider receipt."
            line = process.stdout.readline()
        assert line, "Allocation child exited before its provider receipt."
        status = json.loads(line)
        assert status["ttl"] == 60
        data = status["created"]
        ref = ComputerRef(
            data["provider"],
            data["scope"],
            data["resource_id"],
            ComputerIdentity(**data["identity"]),
        )
        refs.append(ref)
        assert compute.status(ref).state == "running"
        assert source_record() == {
            "scope": compute.scope,
            "allocation_id": ref.identity.allocation_id,
            "state": "creating",
        }
        survivor = allocate_account("survivor")
        other = allocate(survivor)
        digest = other.call(
            other.owner,
            "run",
            {
                "command": "python3 - <<'PY'\nfrom pathlib import Path\nimport os,hashlib\nd=os.urandom(1031);Path('state.bin').write_bytes(d);print(hashlib.sha256(d).hexdigest())\nPY",
                "timeout": 10,
            },
        )
        assert digest["exit_code"] == 0
        digest = digest["output"].strip()
        assert len(bytes.fromhex(digest)) == hashlib.sha256().digest_size
        contender = allocate_account("contender")
        with pytest.raises(DesktopError, match="busy"):
            allocate(contender)
        assert occupied() == 2
        assert AllocationLedger(contender[2], contender[0]).get(contender[1]) is None

        process.kill()
        assert process.wait(timeout=10) == -signal.SIGKILL
        killed = time.monotonic()
        with pytest.raises(WorkerLeaseLost):
            WorkerLease(store, owner).acquire()
        assert compute.status(ref).state == "running"
        deadline = time.monotonic() + 70
        while remaining_lease() > 0:
            assert time.monotonic() < deadline, "Normal account lease did not expire."
            time.sleep(0.25)
        # Lease expiry by itself must never free billable resource capacity.
        with pytest.raises(DesktopError, match="busy"):
            allocate(contender)
        assert occupied() == 2 and source_record()["state"] == "creating"
        assert compute.status(ref).state == "running"

        recovery_factory = CloudDesktopFactory(
            owner, compute, SPEC, tmp_path / "fresh-controller", store=store, max_computers=2
        )
        desktops = DesktopComputers(owner, ComputerViews(), factory=recovery_factory, store=store)

        def no_model(_):
            raise AssertionError("Recovery must not replay the interrupted task.")

        runtime = BotRuntime(store, owner, no_model, desktop_computers=desktops)
        runtime.start()
        assert runtime._shared_lease.epoch == 2 and runtime._shared_lease.ttl == 60
        assert compute.status(ref).state == "missing"
        assert source_record()["state"] == "deleted" and occupied() == 1
        assert store.task(owner, queued["id"])["status"] == "interrupted"
        replacement = allocate(contender)
        assert replacement.ref.resource_id != ref.resource_id and occupied() == 2
        preserved = other.call(
            other.owner, "run", {"command": "sha256sum state.bin", "timeout": 10}
        )
        assert preserved["exit_code"] == 0 and preserved["output"].split()[0] == digest
        empty = replacement.call(
            replacement.owner, "run", {"command": "test ! -e state.bin", "timeout": 10}
        )
        assert empty["exit_code"] == 0
        with store.connect() as db:
            assert db.execute("SELECT count(*) FROM provider_calls").fetchone()[0] == 0
        receipt.update(
            passed=True,
            account_lease_seconds=60,
            recovery_seconds=round(time.monotonic() - killed, 3),
            worker_epoch_after_recovery=2,
            capacity_before_and_after_lease_expiry=2,
            capacity_after_reconciliation=1,
            capacity_after_new_account_allocation=2,
            uncertain_task_status="interrupted",
            actual_provider_discovery_without_durable_resource_id=True,
            survivor_workspace_sha256=digest,
        )
    finally:
        errors = []
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        if runtime:
            try:
                runtime.stop()
            except Exception:
                errors.append("runtime_stop")
        for surface in reversed(surfaces):
            try:
                surface.close()
            except Exception:
                errors.append("surface_close")
        # A child may have created a computer before its diagnostic pipe failed.
        # Discover only this test's durable identity, never a broad Docker sweep.
        record = source_record()
        if record:
            identity = ComputerIdentity(owner, bot, record["allocation_id"])
            found = compute.find(identity)
            if found and found not in refs:
                refs.append(found)
        states = []
        for ref in refs:
            try:
                state = compute.status(ref).state
                if state == "running":
                    compute.stop(ref)
                    state = compute.status(ref).state
                if state == "stopped":
                    compute.delete(ref)
                state = compute.status(ref).state
                states.append({"id": ref.resource_id, "state": state})
                if state != "missing":
                    errors.append("computer_cleanup")
            except Exception:
                errors.append("computer_cleanup")
        for lease in reversed(leases):
            try:
                lease.close()
            except Exception:
                errors.append("lease_close")
        receipt.update(
            computers=states,
            cleanup_errors=errors,
            child_processes_stopped=all(p.poll() is not None for p in processes),
            runtime_stopped=runtime is None or not runtime.is_running,
        )
        receipt["passed"] = receipt["passed"] and not errors
        path = os.environ.get("ALYSIS_BOTS_COMPUTE_RECOVERY_RECEIPT")
        if path:
            Path(path).write_text(json.dumps(receipt, indent=2) + "\n")
        assert not errors, "Study cleanup failed; inspect its private receipt."


if __name__ == "__main__":
    try:
        child(json.loads(sys.stdin.readline()))
    except Exception as error:
        # Do not print bootstrap data or upstream connection details.
        raise SystemExit(type(error).__name__) from None
