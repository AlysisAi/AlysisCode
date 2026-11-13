"""Real local database loss during a bot's Linux command.

Uses a new private PostgreSQL container and volume, real runtime and Linux
computers, and an explicitly scripted model fixture (zero external model calls).
Existing databases, previews and cloud resources are never used or restarted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import secrets
import socket
import subprocess
import threading
import time
import traceback
from pathlib import Path
from uuid import uuid4

import bots_hosted_database as database
from psycopg.conninfo import make_conninfo

from alysis_code.bots.cloud_desktop import CloudDesktopFactory, CloudDesktopFiles
from alysis_code.bots.compute import ComputerIdentity, ComputerRef, ComputerSpec
from alysis_code.bots.compute_docker import DockerComputers
from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.desktop import DesktopComputers
from alysis_code.bots.models import BotInput, FileInput, TaskInput
from alysis_code.bots.object_storage import LocalObjectStorage
from alysis_code.bots.postgres import PostgresStore
from alysis_code.bots.proposals import ActionDecision, ActionProposal, ProposalStore
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.worker_lease import WorkerLease
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMResponse, ToolCall

ROOT = Path(__file__).resolve().parents[2]


def until(read, accept=bool, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = read()
        if accept(result):
            return result
        time.sleep(0.1)
    raise RuntimeError("Outage study condition missed its deadline.")


class CommandFixture:
    """Test stimulus only, not a production model or behavioral shortcut."""

    model = "explicit-outage-fixture"
    supports_tool_calling = True

    def __init__(self, command):
        self.command, self.calls, self.result = command, 0, None

    def chat(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                "",
                [ToolCall("command", "computer_run", {"command": self.command, "timeout": 30})],
                {},
            )
        self.result = json.loads(kwargs["messages"][-1]["content"])
        return LLMResponse("Fixture completed its command.", [], {})


def run(output):
    output.mkdir(parents=True, exist_ok=False)
    logging.getLogger("psycopg.pool").setLevel(logging.CRITICAL)
    name = "alysis-bots-outage-" + uuid4().hex[:12]
    volume = name + "-data"
    owner = name
    password = secrets.token_urlsafe(40)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    dsn = make_conninfo(
        host="127.0.0.1",
        port=port,
        user="alysis",
        dbname="alysis_bots",
        password=password,
        connect_timeout=2,
    )
    provider = DockerComputers(
        "desktop-linux", namespace_profile=ROOT / "scripts/cloud-dev/seccomp.json"
    )
    receipt = {
        "passed": False,
        "kind": "Actual PostgreSQL outage and Linux; scripted model fixture",
        "external_model_calls": 0,
        "cloud_resources": 0,
        "database": name,
        "checks": {},
        "computers": [],
    }
    stores, runtimes, refs = [], [], []
    database_started = False

    def save():
        (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")

    def docker(*args):
        result = subprocess.run(
            ["docker", "--context", "desktop-linux", *args], capture_output=True, timeout=90
        )
        if result.returncode:
            raise RuntimeError("Isolated outage study Docker operation failed.")
        return result.stdout

    def store_at(label):
        store = PostgresStore(
            dsn, objects=LocalObjectStorage(output / "objects"), state_directory=output / label
        )
        stores.append(store)
        return store

    def desktop_at(store, label):
        factory = CloudDesktopFactory(
            owner,
            provider,
            ComputerSpec("alysis-bots-shared-state-20261020:qa"),
            output / label,
            persist_workspace=True,
            store=store,
        )
        return DesktopComputers(
            owner, ComputerViews(), factory=factory, store=store, file_transfer=CloudDesktopFiles()
        )

    def approved_task(store, chat, message):
        store.enqueue(owner, chat, TaskInput(message=message, request_id=uuid4().hex))
        task = store.claim(owner)
        proposals = ProposalStore(store, owner)
        proposal = proposals.propose(
            task,
            ActionProposal(
                question="Run the synthetic private computer check?",
                instructions=message,
                offline_computer=True,
            ),
        )
        store.finish(owner, task["id"], "completed", content="Fixture permission proposal")
        return proposals.decide(
            task["bot_id"],
            proposal["id"],
            ActionDecision(
                action="accept",
                expected_revision=proposal["revision"],
                offline_computer=True,
            ),
        )["task_id"]

    def remember_ref(store, bot):
        with store.connect() as db:
            row = db.execute(
                "SELECT record FROM computer_allocations WHERE owner=? AND bot_id=?", (owner, bot)
            ).fetchone()
        if not row:
            return None
        record = json.loads(row[0])
        if not record.get("resource_id"):
            return None
        ref = ComputerRef(
            record["provider"],
            record["scope"],
            record["resource_id"],
            ComputerIdentity(owner, bot, record["allocation_id"]),
        )
        if ref not in refs:
            refs.append(ref)
        return ref

    try:
        docker("volume", "create", volume)
        docker(
            "run",
            "--detach",
            "--name",
            name,
            "--cpus",
            "1",
            "--memory",
            "768m",
            "--pids-limit",
            "128",
            "--publish",
            f"127.0.0.1:{port}:5432",
            "--mount",
            "type=volume,source=" + volume + ",target=/var/lib/postgresql",
            "--env",
            "PGDATA=" + database.PGDATA,
            "--entrypoint",
            "bash",
            database.IMAGE,
            *database.server_command()[1:],
        )
        database_started = True
        database.configure(name, password)
        store = store_at("first")
        bot = store.save_bot(owner, BotInput(name="Outage study", purpose="Synthetic recovery"))
        chat = store.create_chat(owner, bot["id"], "Continuous")
        data = "Saved before outage — 東京 Αθήνα".encode()
        library = store.save_file(
            owner, bot["id"], FileInput(name="original.txt", content=data.decode())
        )
        lease = WorkerLease(store, owner)
        worker = lease.acquire()
        desktop = desktop_at(worker, "baseline-ledger")
        store.enqueue(
            owner, chat["id"], TaskInput(message="Baseline checkpoint", request_id=uuid4().hex)
        )
        task = worker.claim(owner)
        baseline = desktop.call(
            task,
            "computer_run",
            {
                "command": "python3 - <<'PY'\nfrom pathlib import Path\nimport os,hashlib\np=Path('archive/東京');p.mkdir(parents=True)\nd=os.urandom(73);(p/'state.bin').write_bytes(d)\nprint(hashlib.sha256(d).hexdigest())\nPY",
                "timeout": 10,
            },
            EventCancellationToken(threading.Event()),
            approved=True,
        )
        assert baseline["exit_code"] == 0
        workspace_hash = baseline["output"].strip()
        remember_ref(store, bot["id"])
        desktop.release(task["id"], cancelled=False)
        worker.finish(owner, task["id"], "completed", content="Baseline stored")
        desktop.close()
        lease.close()

        task_id = approved_task(store, chat["id"], "Run the bounded outage command")
        fixture = CommandFixture(
            "printf started > phase.txt; sleep 25; printf forbidden > late.txt"
        )
        runtime = BotRuntime(
            store, owner, lambda _: fixture, desktop_computers=desktop_at(store, "active-ledger")
        )
        runtimes.append(runtime)
        runtime.start()

        def running_command():
            ref = remember_ref(store, bot["id"])
            if ref is None or provider.status(ref).state != "running":
                return None
            root = f"/tmp/alysis-cloud-{ref.identity.allocation_id}/workspace/"
            probe = provider.run(ref, ["/usr/bin/test", "-f", root + "phase.txt"], timeout=5)
            return ref if probe.exit_code == 0 else None

        ref = until(running_command)
        started = time.monotonic()
        docker("stop", "--time", "0", name)
        until(lambda: provider.status(ref).state == "missing", seconds=22)
        receipt["checks"]["computer_retired_while_database_off"] = round(
            time.monotonic() - started, 3
        )
        assert not runtime.is_running
        assert json.loads(docker("inspect", name))[0]["State"]["Running"] is False
        assert fixture.calls == 1  # No follow-up model call or replay after losing authority.
        save()

        # Keep the database offline until every old lane has exited. Restoration
        # must not rescue an in-flight result write and make this a shorter test.
        until(
            lambda: all(
                t is None or not t.is_alive()
                for t in (
                    runtime._thread,
                    runtime._scheduler,
                    runtime._background_thread,
                    runtime._peer_thread,
                )
            ),
            seconds=30,
        )
        receipt["checks"]["all_worker_lanes_stopped_while_database_off"] = True
        docker("start", name)
        database.wait_ready(name)
        reconnect_started = time.monotonic()

        def recovered_read():
            # PostgreSQL being ready does not mean its old connection pool has
            # completed reconnection backoff. Probe only reads; never replay a
            # failed write. The service's health endpoint has the same dependency.
            try:
                return store.bot(owner, bot["id"])["id"] == bot["id"]
            except RuntimeError:
                return False

        until(recovered_read, seconds=45)
        receipt["checks"]["pool_read_recovery_seconds"] = round(
            time.monotonic() - reconnect_started, 3
        )
        # Release the old epoch normally; replacement uses fresh scratch.
        runtime.stop()
        replacement = store_at("replacement")
        verify = CommandFixture(
            "sha256sum archive/東京/state.bin; test ! -e phase.txt; test ! -e late.txt"
        )
        restarted = BotRuntime(
            replacement,
            owner,
            lambda _: verify,
            desktop_computers=desktop_at(replacement, "replacement-ledger"),
        )
        runtimes.append(restarted)
        restarted.start()
        assert replacement.task(owner, task_id)["status"] == "interrupted"
        assert verify.calls == 0
        assert b"".join(replacement.file_chunks(owner, bot["id"], library["id"])) == data
        receipt["checks"]["interrupted_without_replay"] = True
        receipt["checks"]["library_sha256"] = hashlib.sha256(data).hexdigest()
        restarted.stop()
        verify_task = approved_task(replacement, chat["id"], "Verify the last committed workspace")
        restarted = BotRuntime(
            replacement,
            owner,
            lambda _: verify,
            desktop_computers=desktop_at(replacement, "verify-ledger"),
        )
        runtimes.append(restarted)
        restarted.start()
        until(
            lambda: (
                replacement.task(owner, verify_task)["status"]
                in {"completed", "failed", "interrupted"}
            )
        )
        assert replacement.task(owner, verify_task)["status"] == "completed"
        assert (
            verify.result["exit_code"] == 0 and verify.result["output"].split()[0] == workspace_hash
        )
        remember_ref(replacement, bot["id"])
        receipt["checks"]["committed_workspace_restored_without_uncommitted_files"] = workspace_hash
        receipt["passed"] = True
    except Exception as error:
        receipt["failure_type"] = type(error).__name__
        receipt["failure_location"] = [
            {"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
            for frame in traceback.extract_tb(error.__traceback__)
        ]
        raise
    finally:
        if database_started:
            docker("start", name)
            database.wait_ready(name)
        for runtime in reversed(runtimes):
            try:
                runtime.stop()
            except Exception:
                receipt.setdefault("cleanup_errors", []).append("runtime_stop")
        # Independent provider reconciliation is limited to this study's account.
        for store in stores:
            try:
                with store.connect() as db:
                    bots = db.execute(
                        "SELECT bot_id FROM computer_allocations WHERE owner=?", (owner,)
                    ).fetchall()
                for row in bots:
                    remember_ref(store, row[0])
            except Exception:
                receipt.setdefault("cleanup_errors", []).append("allocation_read")
        for ref in refs:
            if provider.status(ref).state == "running":
                provider.stop(ref)
            if provider.status(ref).state == "stopped":
                provider.delete(ref)
            receipt["computers"].append(
                {"id": ref.resource_id, "state": provider.status(ref).state}
            )
        for store in stores:
            store.close()
        docker("rm", "-f", name)
        docker("volume", "rm", volume)
        receipt["temporary_database_removed"] = True
        cleanup_failed = bool(receipt.get("cleanup_errors")) or any(
            item["state"] != "missing" for item in receipt["computers"]
        )
        receipt["passed"] = receipt["passed"] and not cleanup_failed
        save()
        if cleanup_failed:
            raise RuntimeError("Outage study cleanup was not fully confirmed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-local", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        run(args.output.resolve())
    except Exception:
        raise SystemExit("Database outage study failed; inspect its sanitized receipt.") from None
    print("Database outage qualification passed; zero external model calls.")


if __name__ == "__main__":
    main()
