"""Bounded actual PostgreSQL store load study; no model/provider/VM simulation.

Exercise durable task operations with concurrent accounts and many idle bots.
This measures the store only, not model throughput or production fleet capacity.
Uses the explicitly supplied disposable ALYSIS_BOTS_TEST_POSTGRES_DSN.
"""

import argparse
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.object_storage import LocalObjectStorage
from alysis_code.bots.postgres import PostgresStore
from alysis_code.bots.store import NotFound


def run(output, *, accounts=20, bots_per_account=10, rounds=5):
    if not 2 <= accounts <= 50 or not 1 <= bots_per_account <= 20 or not 1 <= rounds <= 20:
        raise ValueError("Use bounded synthetic load")
    output.mkdir(parents=True, exist_ok=True)
    store = PostgresStore(
        os.environ["ALYSIS_BOTS_TEST_POSTGRES_DSN"],
        objects=LocalObjectStorage(output / "objects"),
        state_directory=output / "state",
    )
    samples, failures, claims = [], [], []
    lock = threading.Lock()
    prefix = "load-" + uuid4().hex
    owners = [f"{prefix}-{index}" for index in range(accounts)]

    def measure(label, call, *args, **kwargs):
        started = time.monotonic()
        try:
            return call(*args, **kwargs)
        except Exception as error:
            with lock:
                failures.append(
                    {
                        "operation": label,
                        "type": type(error).__name__,
                        "cause": type(error.__context__).__name__,
                        "sqlstate": getattr(error.__context__, "sqlstate", None),
                    }
                )
            raise
        finally:
            with lock:
                samples.append((label, time.monotonic() - started))

    def seed(owner):
        result = []
        for index in range(bots_per_account):
            bot = measure(
                "create_bot",
                store.save_bot,
                owner,
                BotInput(name=f"Study {index}", purpose="Synthetic load qualification"),
            )
            chat = store.create_chat(owner, bot["id"], "Continuous")
            result.append((bot["id"], chat["id"]))
        return result

    with ThreadPoolExecutor(max_workers=accounts) as pool:
        bots = dict(zip(owners, pool.map(seed, owners), strict=True))
    barrier = threading.Barrier(accounts)

    def work(owner):
        barrier.wait(timeout=30)
        for round_index in range(rounds):
            queued = []
            for _bot_id, chat_id in bots[owner]:
                request = TaskInput(
                    message=f"Synthetic request {round_index}", request_id=uuid4().hex
                )
                task = measure("enqueue", store.enqueue, owner, chat_id, request)
                repeated = measure("idempotent_enqueue", store.enqueue, owner, chat_id, request)
                assert repeated["id"] == task["id"]
                queued.append(task["id"])
            for _ in queued:
                task = measure("claim", store.claim, owner)
                assert task and task["id"] in queued
                with lock:
                    claims.append(task["id"])
                measure(
                    "finish",
                    store.finish,
                    owner,
                    task["id"],
                    "completed",
                    content="Store fixture only",
                )
                result = measure("read_task", store.task, owner, task["id"])
                assert result["status"] == "completed"
                assert [e["kind"] for e in result["events"]] == ["queued", "running", "completed"]
            assert store.claim(owner) is None

    started = time.monotonic()
    failure = None
    try:
        with ThreadPoolExecutor(max_workers=accounts) as pool:
            list(pool.map(work, owners))
    except Exception as error:
        failure = error
    elapsed = time.monotonic() - started
    if failure is None:
        assert len(claims) == len(set(claims)) == accounts * bots_per_account * rounds
    for index, owner in enumerate(owners):
        other = owners[(index + 1) % len(owners)]
        try:
            store.bot(owner, bots[other][0][0])
        except NotFound:
            pass
        else:
            raise AssertionError("Cross-account bot lookup succeeded")
    latencies = {}
    for label in sorted({label for label, _ in samples}):
        values = sorted(v * 1000 for key, v in samples if key == label)
        latencies[label] = {
            "count": len(values),
            "p50_ms": round(values[int((len(values) - 1) * 0.50)], 2),
            "p95_ms": round(values[int((len(values) - 1) * 0.95)], 2),
            "max_ms": round(values[-1], 2),
        }
    result = {
        "passed": failure is None and not failures,
        "evidence": "actual PostgreSQL store; no models or task computers",
        "accounts": accounts,
        "bots": accounts * bots_per_account,
        "tasks": len(claims),
        "elapsed_seconds": round(elapsed, 2),
        "tasks_per_second": round(len(claims) / elapsed, 2),
        "model_calls": 0,
        "duplicate_claims": len(claims) - len(set(claims)),
        "cross_account_checks": accounts,
        "operations": latencies,
        "failures": failures,
    }
    (output / "receipt.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--accounts", type=int, default=20)
    parser.add_argument("--bots-per-account", type=int, default=10)
    parser.add_argument("--rounds", type=int, default=5)
    args = parser.parse_args()
    print(
        json.dumps(
            run(
                args.output,
                accounts=args.accounts,
                bots_per_account=args.bots_per_account,
                rounds=args.rounds,
            )
        )
    )
