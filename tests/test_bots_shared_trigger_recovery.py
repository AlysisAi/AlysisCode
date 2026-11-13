"""Actual shared database, upload protocol and controller crash qualification.

The model intentionally attempts an unavailable write as an explicit fixture.
No model/provider/cloud calls and no external connector events are used.
"""

import base64
import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from test_bots_backup import databases  # noqa: F401
from test_bots_shared_scheduler_recovery import until

from alysis_code.bots.models import BotInput
from alysis_code.bots.object_storage import LocalObjectStorage
from alysis_code.bots.postgres import PostgresStore
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import NotFound
from alysis_code.bots.transfers import FileTransfers, UploadChunk, UploadInput
from alysis_code.bots.triggers import TriggerDecision, TriggerInput, TriggerStore
from alysis_code.bots.worker_lease import WorkerLease
from alysis_code.llm.types import LLMResponse, ToolCall


def child(config):
    store = PostgresStore(
        config["dsn"],
        objects=LocalObjectStorage(Path(config["objects"])),
        state_directory=Path(config["scratch"]),
    )

    class ReviewFixture:
        model = "explicit-trigger-review-fixture"
        supports_tool_calling = True

        def __init__(self):
            self.calls = 0

        def chat(self, **kwargs):
            self.calls += 1
            names = {t["function"]["name"] for t in kwargs["tools"]}
            assert not names & {
                "save_file",
                "coding_task",
                "computer_run",
                "browser_navigate",
                "create_trigger",
                "create_schedule",
                "save_working_note",
            }
            if self.calls == 1:
                # Even a fabricated tool call outside the advertised schema
                # must not acquire write authority from an uploaded file.
                return LLMResponse(
                    "",
                    [
                        ToolCall(
                            "denied",
                            "save_file",
                            {
                                "name": "unapproved.txt",
                                "content": "Must not be saved",
                            },
                        )
                    ],
                    {},
                )
            assert "Invalid tool arguments" in kwargs["messages"][-1]["content"]
            if config["block"]:
                token = kwargs["cancellation_token"]
                while not token.wait(0.1):
                    pass
                token.throw_if_cancelled()
            return LLMResponse("Scripted event review finished.", [], {})

    stopping = False

    def request_stop(*_):
        nonlocal stopping
        # A Python signal handler may interrupt Event.wait while its lock is
        # held. Only assign a flag here; shutdown runs outside the handler.
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    runtime = BotRuntime(store, config["owner"], lambda _: ReviewFixture())
    try:
        runtime.start()
        print(json.dumps({"ready": True, "epoch": runtime._shared_lease.epoch}), flush=True)
        while not stopping:
            time.sleep(0.1)
            if not runtime.is_running:
                raise AssertionError("Study coordinator stopped unexpectedly.")
    finally:
        runtime.stop()
        store.close()


def test_file_events_deduplicate_and_recover_without_replaying_uncertain_review(
    databases,  # noqa: F811
    tmp_path,
):
    root = tmp_path / "objects"
    store = databases(objects=LocalObjectStorage(root))
    processes, groups, files = [], [], []
    receipt = {
        "passed": False,
        "kind": "Actual PostgreSQL/upload protocol/controller processes; scripted model",
        "external_model_calls": 0,
        "computer_provider_calls": 0,
    }

    def start(owner, *, block=False):
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
                    "objects": str(root),
                    "owner": owner,
                    "scratch": str(tmp_path / uuid4().hex),
                    "block": block,
                }
            )
            + "\n"
        )
        process.stdin.close()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(20), "Study controller did not report readiness."
            line = process.stdout.readline()
        assert line, "Study controller exited before readiness."
        state = json.loads(line)
        assert state["ready"] is True
        return process, state

    def stop(process):
        if process.poll() is None:
            process.terminate()
            try:
                assert process.wait(timeout=25) == 0
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
                raise AssertionError("Study controller did not stop cleanly.") from None

    def upload(owner, bot):
        data = os.urandom(8007)
        name = "資料-" + uuid4().hex + ".bin"
        sender = FileTransfers(store, owner)
        request = UploadInput(name=name, size=len(data), request_id=uuid4().hex)
        item = sender.begin(bot, request)
        assert sender.begin(bot, request)["id"] == item["id"]
        part = UploadChunk(offset=0, data_base64=base64.b64encode(data).decode())
        sender.append(bot, item["id"], part)
        sender.append(bot, item["id"], part)
        saved = sender.complete(bot, item["id"])
        assert FileTransfers(store, owner).complete(bot, item["id"]) == saved
        assert FileTransfers(store, owner).complete(bot, item["id"]) == saved
        file = {
            "owner": owner,
            "bot": bot,
            "id": saved["file_id"],
            "sha256": hashlib.sha256(data).hexdigest(),
            "deleted": False,
        }
        files.append(file)
        return file

    def deliveries(owner, bot):
        return TriggerStore(store, owner).list(bot)[0]["recent_deliveries"]

    def task_ids(owner, bot):
        return {d["task_id"] for d in deliveries(owner, bot) if d["task_id"]}

    def states(group):
        return [
            store.task(group["owner"], task)
            for bot in group["bots"]
            for task in task_ids(group["owner"], bot)
        ]

    try:
        for _ in range(2):
            owner = "trigger-study-" + uuid4().hex
            group = {"owner": owner, "bots": [], "controls": []}
            groups.append(group)
            triggers = TriggerStore(store, owner)
            for n in range(4):
                bot = store.save_bot(
                    owner, BotInput(name=f"Review {n}", purpose="Synthetic file events")
                )["id"]
                draft = triggers.create(
                    bot,
                    TriggerInput(
                        name="Review additions",
                        instructions="Review added files; suggest a next action if useful.",
                    ),
                )
                active = triggers.decide(
                    bot, draft["id"], TriggerDecision(state="active", expected_revision=1)
                )
                added = [upload(owner, bot) for _ in range(3 if n < 2 else 1)]
                if n < 2:
                    group["bots"].append(bot)
                else:
                    group["controls"].append(bot)
                    if n == 2:
                        paused = triggers.decide(
                            bot,
                            active["id"],
                            TriggerDecision(state="paused", expected_revision=active["revision"]),
                        )
                        triggers.decide(
                            bot,
                            active["id"],
                            TriggerDecision(state="active", expected_revision=paused["revision"]),
                        )
                    else:
                        store.delete_item(owner, bot, added[0]["id"], kind="files")
                        added[0]["deleted"] = True
        # Real collection-window expiry, with no controllers running yet.
        with store.connect() as db:
            newest = db.execute("SELECT max(created_at) FROM trigger_deliveries").fetchone()[0]
        until(time.time, lambda now: now >= newest + 5.1, seconds=10)
        owners = [g["owner"] for g in groups]
        with ThreadPoolExecutor(max_workers=6) as pool:
            counts = list(
                pool.map(
                    lambda owner: TriggerStore(store, owner).dispatch(max_calls=20), owners * 6
                )
            )
        assert sum(counts) == 4
        for group in groups:
            for bot in group["bots"]:
                assert len(deliveries(group["owner"], bot)) == 3
                assert len(task_ids(group["owner"], bot)) == 1
            for bot in group["controls"]:
                assert {d["state"] for d in deliveries(group["owner"], bot)} == {"skipped"}
                assert not task_ids(group["owner"], bot)
        victim, first = start(owners[0], block=True)
        start(owners[1])

        def running():
            return next(
                (t for t in states(groups[0]) if t["status"] == "running" and t["calls"] == 2), None
            )

        uncertain = until(running)
        original_events = {e["file_id"] for e in uncertain["config"]["activation"]["events"]}
        newer = {upload(owners[0], uncertain["bot_id"])["id"] for _ in range(2)}
        assert (
            len([d for d in deliveries(owners[0], uncertain["bot_id"]) if d["state"] == "pending"])
            == 2
        )
        victim.kill()
        victim.wait(timeout=10)
        killed_at = time.monotonic()
        until(
            lambda: states(groups[1]),
            lambda tasks: len(tasks) == 2 and all(t["status"] == "completed" for t in tasks),
        )

        def expired():
            with store.connect() as db:
                return db.execute(
                    "SELECT expires FROM worker_leases WHERE owner=?", (owners[0],)
                ).fetchone()[0] <= WorkerLease.now(db)

        until(expired, seconds=70)
        _, second = start(owners[0])
        assert second["epoch"] > first["epoch"]
        until(
            lambda: states(groups[0]),
            lambda tasks: (
                len(tasks) == 3 and all(t["status"] in {"completed", "interrupted"} for t in tasks)
            ),
        )
        receipt["recovery_seconds"] = round(time.monotonic() - killed_at, 3)
        old = store.task(owners[0], uncertain["id"])
        assert old["status"] == "interrupted" and old["calls"] == 2
        assert {e["file_id"] for e in old["config"]["activation"]["events"]} == original_events
        fresh_id = (task_ids(owners[0], uncertain["bot_id"]) - {uncertain["id"]}).pop()
        fresh = store.task(owners[0], fresh_id)
        assert fresh["status"] == "completed"
        assert {e["file_id"] for e in fresh["config"]["activation"]["events"]} == newer
        for group in groups:
            for task in states(group):
                assert task["calls"] == 2
                assert any(
                    e["kind"] == "tool_failed" and e["data"]["name"] == "save_file"
                    for e in task["events"]
                )
            for bot in group["bots"] + group["controls"]:
                expected = {f["id"] for f in files if f["bot"] == bot and not f["deleted"]}
                assert {f["id"] for f in store.list_files(group["owner"], bot)} == expected
            with pytest.raises(NotFound):
                TriggerStore(store, group["owner"]).list(
                    next(g for g in groups if g is not group)["bots"][0]
                )
        for file in files:
            if not file["deleted"]:
                assert (
                    hashlib.sha256(
                        store.file_data(file["owner"], file["bot"], file["id"])
                    ).hexdigest()
                    == file["sha256"]
                )

        def counts():
            with store.connect() as db:
                return {
                    table: db.execute("SELECT count(*) FROM " + table).fetchone()[0]
                    for table in (
                        "tasks",
                        "messages",
                        "files",
                        "activation_events",
                        "trigger_deliveries",
                        "provider_calls",
                    )
                }

        for process in processes:
            stop(process)
        before = counts()
        assert before["tasks"] == 5 and before["files"] == 16
        assert before["activation_events"] == before["trigger_deliveries"] == 18
        assert before["provider_calls"] == 10
        for owner in owners:
            start(owner)
        time.sleep(3.2)
        for process in processes:
            stop(process)
        assert counts() == before
        with store.connect() as db:
            assert (
                db.execute("SELECT count(*) FROM provider_calls WHERE status='unknown'").fetchone()[
                    0
                ]
                == 1
            )
        receipt.update(
            passed=True,
            accounts=2,
            bots=8,
            uploads=18,
            retained_files=16,
            completed_reviews=4,
            interrupted_reviews=1,
            skipped_deliveries=4,
            fixture_calls=10,
            unknown_fixture_calls=1,
            counts=before,
            original_epoch=first["epoch"],
            recovered_epoch=second["epoch"],
            no_unauthorized_files=True,
            unchanged_after_restart=True,
            original_events_not_replayed=True,
            later_events_reviewed=True,
        )
    finally:
        for process in processes:
            stop(process)
            process.stdout.close()
        receipt["children_stopped"] = all(p.poll() is not None for p in processes)
        output = os.environ.get("ALYSIS_BOTS_TRIGGER_RECEIPT")
        if output:
            path = Path(output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    assert sys.argv[1:] == ["--worker"]
    child(json.loads(sys.stdin.readline()))
