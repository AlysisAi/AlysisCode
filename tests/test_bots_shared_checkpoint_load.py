"""Real PostgreSQL checkpoint-pruning contention; explicit synthetic snapshots.

No model/cloud/VM calls. Checkpoint bytes are small fixtures, created with the
actual workspace/store API; eligibility uses the minimum policy and a future
test clock. This measures metadata cleanup, not production snapshot retention.
"""

import json
import os
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from test_bots_backup import databases  # noqa: F401

from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.postgres import Connection
from alysis_code.bots.retention import ObjectRetention, RetentionPolicy
from alysis_code.bots.workspace_objects import WorkspaceObjects


@pytest.mark.skipif(
    not os.environ.get("ALYSIS_BOTS_CHECKPOINT_LOAD_RECEIPT"),
    reason="Explicit bounded checkpoint inventory study requested",
)
def test_checkpoint_pruning_contention(databases, monkeypatch):  # noqa: F811
    store = databases()
    ws = WorkspaceObjects(store)
    other = store.save_bot("interactive", BotInput(name="Other", purpose="Write probe"))
    receipt = {
        "passed": False,
        "kind": "Actual local PostgreSQL/workspace store; synthetic snapshot bytes and future clock",
        "external_model_calls": 0,
        "cloud_calls": 0,
        "samples": [],
    }
    entered = threading.Event()
    cleanup_thread = [None]
    counts = Counter()
    original_execute = Connection.execute

    def observed(db, sql, args=None):
        if threading.get_ident() == cleanup_thread[0]:
            entered.set()  # Actual shared write lock is already acquired.
            counts[" ".join(sql.split()[:3])] += 1
        return original_execute(db, sql, args)

    monkeypatch.setattr(Connection, "execute", observed)
    policy = RetentionPolicy(300, 300, 2, 300)
    try:
        for count in (300, 3000):
            owner = f"checkpoint-study-{count}"
            bot = store.save_bot(owner, BotInput(name="Snapshots", purpose="Pruning load"))
            bot_id = bot["id"]
            chat = store.create_chat(owner, bot_id, "Continuous")
            saved = []
            for index in range(count + 1):
                task = store.enqueue(
                    owner,
                    chat["id"],
                    TaskInput(message="Synthetic checkpoint", request_id=uuid4().hex),
                )
                assert store.claim(owner)["id"] == task["id"]
                data = f"snapshot fixture Αθήνα 東京 {index}".encode()
                ref = ws.stage(owner, bot_id, task["id"], [data], len(data))
                saved.append((ref, data))
                if index < count:
                    store.finish(owner, task["id"], "completed", publish_reply=False)
            now = time.time() + 301
            counts.clear()
            entered.clear()

            def sweep(owner=owner, now=now):
                cleanup_thread[0] = threading.get_ident()
                start = time.monotonic()
                try:
                    result = ObjectRetention(store, owner, policy).sweep(now=now, limit=1)
                    return result, time.monotonic() - start
                finally:
                    cleanup_thread[0] = None

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(sweep)
                assert entered.wait(10)
                start = time.monotonic()
                store.save_bot(
                    "interactive",
                    BotInput(name="Other", purpose=f"Write during {count}"),
                    bot_id=other["id"],
                )
                write_seconds = time.monotonic() - start
                result, seconds = future.result(timeout=30)
            assert result == {"deleted": 0, "pending": 0}
            with store.connect() as db:
                kept = {
                    r[0]
                    for r in db.execute(
                        "SELECT ref FROM workspace_objects WHERE bot_id=?", (bot_id,)
                    ).fetchall()
                }
                assert kept == {ref.encode() for ref, _ in saved[-3:]}
                orphaned = db.execute(
                    "SELECT count(*) FROM stored_objects WHERE owner=? AND unreferenced_at=?",
                    (owner, now),
                ).fetchone()[0]
                assert orphaned == count - 2
            for ref, data in saved:
                assert b"".join(store.objects.read(ref)) == data
            sample = {
                "completed_checkpoints": count,
                "active_checkpoints": 1,
                "metadata_pruned": count - 2,
                "sweep_seconds": seconds,
                "other_account_write_seconds": write_seconds,
                "statements": dict(counts),
                "retained_latest_two_and_active": True,
                "all_snapshot_bytes_retained_for_full_grace": True,
            }
            receipt["samples"].append(sample)
            print(json.dumps(sample), flush=True)
        receipt["passed"] = True
    finally:
        target = Path(os.environ["ALYSIS_BOTS_CHECKPOINT_LOAD_RECEIPT"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(receipt, indent=2) + "\n")
