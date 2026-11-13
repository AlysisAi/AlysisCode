"""Bounded real PostgreSQL/account-worker load; explicitly scripted model.

Measures live runtime polling and concurrent task coordination, not model speed,
API throughput, cloud capacity or an availability guarantee.
"""

import json
import os
import resource
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from test_bots_backup import databases  # noqa: F401
from test_bots_shared_scheduler_recovery import until

from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.runtime import BotRuntime
from alysis_code.llm.types import LLMResponse


def test_shared_account_workers_remain_available_under_bounded_load(
    databases,  # noqa: F811
    tmp_path,
):
    store = databases()
    runtimes, groups, tasks = [], {}, []
    blocked_entered = threading.Event()
    fixture_lock = threading.Lock()
    fixture_calls = []
    owners = [f"runtime-load-{uuid4().hex}" for _ in range(12)]
    receipt = {
        "passed": False,
        "kind": "Actual PostgreSQL and live account workers; explicit model fixture",
        "external_model_calls": 0,
        "computer_provider_calls": 0,
        "accounts": len(owners),
        "bots_per_account": 25,
    }

    def seed(owner):
        bots = []
        for index in range(receipt["bots_per_account"]):
            bot = store.save_bot(
                owner, BotInput(name=f"Study {index}", purpose="Synthetic runtime load")
            )
            chat = store.create_chat(owner, bot["id"], "Continuous")
            bots.append((bot["id"], chat["id"]))
        return bots

    with ThreadPoolExecutor(max_workers=4) as pool:
        groups = dict(zip(owners, pool.map(seed, owners), strict=True))
    blocked_bot = groups[owners[0]][0][0]

    class ModelFixture:
        model = "explicit-runtime-load-fixture"
        supports_tool_calling = True

        def __init__(self, bot_id):
            self.bot_id = bot_id

        def chat(self, **kwargs):
            with fixture_lock:
                fixture_calls.append(self.bot_id)
            if self.bot_id == blocked_bot:
                blocked_entered.set()
                token = kwargs["cancellation_token"]
                while not token.wait(0.1):
                    pass
                token.throw_if_cancelled()
            return LLMResponse("Explicit fixture reply, no model inference.", [], {})

    def counts():
        with store.connect() as db:
            return {
                table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("tasks", "messages", "provider_calls")
            }

    def statuses():
        with store.connect() as db:
            return {row[0]: row[1] for row in db.execute("SELECT id,status FROM tasks").fetchall()}

    def completed(ids):
        states = statuses()
        return all(states.get(task_id) == "completed" for task_id in ids)

    try:
        threads_before = threading.active_count()
        started = time.monotonic()
        for owner in owners:
            runtime = BotRuntime(
                store,
                owner,
                lambda task_id, owner=owner: ModelFixture(store.task(owner, task_id)["bot_id"]),
            )
            runtimes.append(runtime)
            runtime.start()
        receipt["startup_seconds"] = round(time.monotonic() - started, 3)
        assert all(runtime.is_running for runtime in runtimes)
        receipt["additional_threads"] = threading.active_count() - threads_before
        before = store._pool.get_stats()
        cpu_before = time.process_time()
        wall_before = time.monotonic()
        time.sleep(8)
        idle_elapsed = time.monotonic() - wall_before
        after = store._pool.get_stats()
        receipt["idle"] = {
            "seconds": round(idle_elapsed, 3),
            "process_cpu_seconds": round(time.process_time() - cpu_before, 3),
            "pool_requests": after["requests_num"] - before["requests_num"],
            "pool_queue_wait_ms": after.get("requests_wait_ms", 0)
            - before.get("requests_wait_ms", 0),
            "pool_size": after["pool_size"],
        }
        assert after["pool_size"] <= 8
        assert all(runtime.is_running for runtime in runtimes)
        assert counts() == {"tasks": 0, "messages": 0, "provider_calls": 0}
        assert not fixture_calls

        blocked = store.enqueue(
            owners[0],
            groups[owners[0]][0][1],
            TaskInput(message="Explicitly blocked fixture call", request_id=uuid4().hex),
        )
        runtimes[0].wake()
        assert blocked_entered.wait(15), "Blocked fixture never started."

        burst_started = time.monotonic()

        def enqueue(owner):
            queued = []
            for bot_id, chat_id in groups[owner][1:7]:
                request = TaskInput(message="Bounded runtime fixture", request_id=uuid4().hex)
                task = store.enqueue(owner, chat_id, request)
                assert store.enqueue(owner, chat_id, request)["id"] == task["id"]
                queued.append((owner, bot_id, chat_id, task["id"]))
            runtimes[owners.index(owner)].wake()
            return queued

        with ThreadPoolExecutor(max_workers=len(owners)) as pool:
            for queued in pool.map(enqueue, owners):
                tasks.extend(queued)
        other_ids = {task_id for owner, _, _, task_id in tasks if owner != owners[0]}
        until(lambda: completed(other_ids), seconds=60)
        receipt["other_accounts_complete_seconds"] = round(time.monotonic() - burst_started, 3)
        assert store.task(owners[0], blocked["id"])["status"] == "running"
        assert all(runtime.is_running for runtime in runtimes)
        assert all(
            store.task(owner, task_id)["status"] == "queued"
            for owner, _, _, task_id in tasks
            if owner == owners[0]
        )

        cancelled_at = time.monotonic()
        runtimes[0].cancel(blocked["id"])
        until(lambda: statuses().get(blocked["id"]) == "cancelled", seconds=15)
        receipt["cancel_seconds"] = round(time.monotonic() - cancelled_at, 3)
        until(lambda: completed([task_id for _, _, _, task_id in tasks]), seconds=30)
        receipt["burst_seconds"] = round(time.monotonic() - burst_started, 3)
        latencies = []
        for owner, bot_id, chat_id, task_id in tasks:
            task = store.task(owner, task_id)
            assert task["status"] == "completed"
            assert [event["kind"] for event in task["events"]] == [
                "queued",
                "running",
                "model_started",
                "completed",
            ]
            messages = store.conversation(owner, chat_id)["messages"]
            assert [message["role"] for message in messages] == ["user", "assistant"]
            latencies.append(task["updated_at"] - task["created_at"])
            assert fixture_calls.count(bot_id) == 1
        latencies.sort()
        assert fixture_calls.count(blocked_bot) == 1
        assert len(fixture_calls) == len(tasks) + 1
        assert all(runtime.is_running for runtime in runtimes)
        final_pool = store._pool.get_stats()
        assert final_pool.get("requests_errors", 0) == 0
        receipt.update(
            tasks=len(tasks),
            cancelled_tasks=1,
            fixture_calls=len(fixture_calls),
            task_latency_p50_seconds=round(latencies[len(latencies) // 2], 3),
            task_latency_p95_seconds=round(latencies[int((len(latencies) - 1) * 0.95)], 3),
            pool_stats=final_pool,
        )
        before_restart = counts()
        for runtime in runtimes:
            runtime.stop()
        assert all(not runtime.is_running for runtime in runtimes)
        receipt["unchanged_on_stop"] = counts() == before_restart
        assert receipt["unchanged_on_stop"]
        with store.connect() as db:
            receipt["call_statuses"] = {
                row[0]: row[1]
                for row in db.execute(
                    "SELECT status,count(*) FROM provider_calls GROUP BY status"
                ).fetchall()
            }
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        receipt["process_peak_rss_mib"] = round(
            peak / (1024 * 1024 if sys.platform == "darwin" else 1024), 2
        )
        receipt["passed"] = True
    finally:
        for runtime in runtimes:
            runtime.stop()
        receipt["workers_stopped"] = all(not runtime.is_running for runtime in runtimes)
        path = Path(os.environ.get("ALYSIS_BOTS_RUNTIME_LOAD_RECEIPT", tmp_path / "receipt.json"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(receipt, indent=2) + "\n")
