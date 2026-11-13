"""Actual PostgreSQL/controller-process scheduling and crash qualification.

Model replies are explicit save-file fixtures. No model, computer-provider or
Azure calls occur. Worker lease expiry uses real database time, never a rewrite.
"""

import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from test_bots_backup import databases  # noqa: F401

from alysis_code.bots.models import BotInput
from alysis_code.bots.object_storage import LocalObjectStorage
from alysis_code.bots.postgres import PostgresStore
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.store import NotFound
from alysis_code.bots.worker_lease import WorkerLease, WorkerLeaseLost
from alysis_code.llm.types import LLMResponse, ToolCall


def until(read, accept=bool, *, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = read()
        if accept(value):
            return value
        time.sleep(0.1)
    raise AssertionError("Shared scheduler study did not reach its expected state.")


def child(config):
    store = PostgresStore(
        config["dsn"],
        objects=LocalObjectStorage(Path(config["objects"])),
        state_directory=Path(config["scratch"]),
    )
    if config["mode"] == "contender":
        lease = WorkerLease(store, config["owner"])
        try:
            lease.acquire()
        except WorkerLeaseLost:
            print(json.dumps({"blocked": True}), flush=True)
        else:
            lease.close()
            raise AssertionError("A second coordinator acquired the live account.")
        finally:
            store.close()
        return

    class SaveFixture:
        model = "explicit-shared-scheduler-fixture"
        supports_tool_calling = True

        def __init__(self):
            self.calls = 0

        def chat(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    "",
                    [
                        ToolCall(
                            "save",
                            "save_file",
                            {"name": "result.txt", "content": config["payload"]},
                        )
                    ],
                    {},
                )
            if config["mode"] == "block":
                token = kwargs["cancellation_token"]
                while not token.wait(0.1):
                    pass
                token.throw_if_cancelled()
            return LLMResponse("Scripted scheduled result saved.", [], {})

    stopping = False

    def request_stop(*_):
        nonlocal stopping
        # A Python signal handler may interrupt Event.wait while its lock is
        # held. Only assign a flag here; shutdown runs outside the handler.
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    runtime = BotRuntime(store, config["owner"], lambda _: SaveFixture())
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


def test_scheduled_runs_survive_real_controller_death_without_replay(
    databases,  # noqa: F811
    tmp_path,
):
    objects_path = tmp_path / "objects"
    store = databases(objects=LocalObjectStorage(objects_path))
    processes, groups = [], []
    receipt = {
        "passed": False,
        "kind": "Actual PostgreSQL/controller processes/local object storage; scripted models",
        "external_model_calls": 0,
        "computer_provider_calls": 0,
    }

    def start(group, mode="normal"):
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        processes.append(process)
        config = {
            "dsn": store._dsn,
            "objects": str(objects_path),
            "scratch": str(tmp_path / uuid4().hex),
            "owner": group["owner"],
            "payload": group["payload"],
            "mode": mode,
        }
        process.stdin.write(json.dumps(config) + "\n")
        process.stdin.close()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(20), "Study worker did not return startup status."
            line = process.stdout.readline()
        assert line, "Study worker exited before startup status."
        status = json.loads(line)
        if mode == "contender":
            assert status == {"blocked": True}
            assert process.wait(timeout=10) == 0
        else:
            assert status["ready"] is True
        return process, status

    def runs(group):
        return [
            store.schedule_runs(group["owner"], bot, schedule)["runs"]
            for bot, schedule in group["scheduled"]
        ]

    def complete(group):
        actual = runs(group)
        return all(
            len(rows) == 1 and rows[0]["current_status"] in {"completed", "interrupted"}
            for rows in actual
        )

    def terminate(process):
        if process.poll() is None:
            process.terminate()
            try:
                assert process.wait(timeout=25) == 0
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
                raise AssertionError("Study worker did not shut down cleanly.") from None

    try:
        due = time.time() + 6
        for index in range(3):
            group = {
                "owner": "scheduler-" + uuid4().hex,
                "payload": "Αθήνα 東京 — " + uuid4().hex,
                "scheduled": [],
            }
            groups.append(group)
            for n in range(8):
                bot = store.save_bot(
                    group["owner"], BotInput(name=f"Routine {index}/{n}", purpose="Synthetic study")
                )["id"]
                at = due if n < 7 else time.time() - 1800
                schedule = store.save_schedule(
                    group["owner"],
                    bot,
                    ScheduleInput(
                        name="Synthetic one-time file",
                        prompt="Save the synthetic fixture's file",
                        timing={"kind": "once", "at": datetime.fromtimestamp(at, UTC)},
                    ),
                    # The historical missed routine is an explicit fixture;
                    # all execution and lease clocks below remain real.
                    now=at - 1,
                )
                if n < 6:
                    group["scheduled"].append((bot, schedule["id"]))
                elif n == 6:
                    store.decide_schedule(group["owner"], bot, schedule["id"], "paused", 1)
                    group["paused"] = (bot, schedule["id"])
                else:
                    group["missed"] = (bot, schedule["id"])
        # Every due event occurs with its account controller absent.
        until(time.time, lambda now: now > due + 0.2, seconds=10)
        started_at = time.time()
        victim, first = start(groups[0], "block")
        for group in groups[1:]:
            start(group)
        start(groups[0], "contender")

        def blocked_task():
            for rows in runs(groups[0]):
                if rows and rows[0]["task_id"]:
                    task = store.task(groups[0]["owner"], rows[0]["task_id"])
                    if task["status"] == "running" and task["calls"] == 2:
                        return task

        uncertain = until(blocked_task)
        saved = store.list_files(groups[0]["owner"], uncertain["bot_id"])
        assert len(saved) == 1
        assert (
            store.file_data(groups[0]["owner"], uncertain["bot_id"], saved[0]["id"])
            == groups[0]["payload"].encode()
        )
        victim.kill()
        victim.wait(timeout=10)
        killed_at = time.monotonic()
        start(groups[0], "contender")
        for group in groups[1:]:
            until(lambda group=group: complete(group))

        def expired():
            with store.connect() as db:
                row = db.execute(
                    "SELECT expires FROM worker_leases WHERE owner=?", (groups[0]["owner"],)
                ).fetchone()
                return row[0] <= WorkerLease.now(db)

        until(expired, seconds=70)
        recovered, second = start(groups[0])
        assert second["epoch"] > first["epoch"]
        until(lambda: complete(groups[0]))
        recovery_seconds = round(time.monotonic() - killed_at, 3)
        for group in groups:
            for bot, schedule in group["scheduled"]:
                actual = store.schedule_runs(group["owner"], bot, schedule)["runs"]
                assert len(actual) == 1
                task = store.task(group["owner"], actual[0]["task_id"])
                assert task["calls"] == 2
                assert task["status"] == (
                    "interrupted" if task["id"] == uncertain["id"] else "completed"
                )
                files = store.list_files(group["owner"], bot)
                assert len(files) == 1
                assert (
                    store.file_data(group["owner"], bot, files[0]["id"])
                    == group["payload"].encode()
                )
                assert len(store.list_chats(group["owner"], bot)) == 1
            assert store.schedule_runs(group["owner"], *group["paused"])["runs"] == []
            missed = store.schedule_runs(group["owner"], *group["missed"])["runs"]
            assert len(missed) == 1 and missed[0]["current_status"] == "missed"
            assert missed[0]["task_id"] is None
            with pytest.raises(NotFound):
                store.list_files(
                    group["owner"], groups[(groups.index(group) + 1) % 3]["scheduled"][0][0]
                )
        failed_schedule = next(s for b, s in groups[0]["scheduled"] if b == uncertain["bot_id"])
        assert (
            next(
                s
                for s in store.list_schedules(groups[0]["owner"], uncertain["bot_id"])
                if s["id"] == failed_schedule
            )["state"]
            == "paused"
        )

        # A clean subsequent restart also must not republish/replay completed
        # or interrupted occurrences. Observe several actual scheduler ticks.
        for process in processes:
            terminate(process)
        with store.connect() as db:
            before = tuple(
                db.execute("SELECT count(*) FROM " + table).fetchone()[0]
                for table in ("tasks", "messages", "provider_calls", "files", "schedule_runs")
            )
        for group in groups:
            start(group)
        time.sleep(3.2)
        for process in processes:
            terminate(process)
        with store.connect() as db:
            after = tuple(
                db.execute("SELECT count(*) FROM " + table).fetchone()[0]
                for table in ("tasks", "messages", "provider_calls", "files", "schedule_runs")
            )
            assert before == after
            unknown = db.execute(
                "SELECT count(*) FROM provider_calls WHERE status='unknown'"
            ).fetchone()[0]
        assert unknown == 1
        receipt.update(
            passed=True,
            accounts=3,
            routines=24,
            completed_tasks=17,
            interrupted_tasks=1,
            missed_occurrences=3,
            paused_without_runs=3,
            preserved_files=18,
            fixture_calls=36,
            unknown_fixture_calls=unknown,
            controller_recovery_seconds=recovery_seconds,
            original_epoch=first["epoch"],
            recovered_epoch=second["epoch"],
            due_to_start_seconds=round(started_at - due, 3),
            unchanged_after_clean_restart=True,
            file_hashes=[hashlib.sha256(g["payload"].encode()).hexdigest() for g in groups],
        )
    finally:
        for process in processes:
            terminate(process)
            if process.stdout:
                process.stdout.close()
        receipt["children_stopped"] = all(p.poll() is not None for p in processes)
        output = os.environ.get("ALYSIS_BOTS_SCHEDULER_RECEIPT")
        if output:
            path = Path(output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    assert sys.argv[1:] == ["--worker"]
    child(json.loads(sys.stdin.readline()))
