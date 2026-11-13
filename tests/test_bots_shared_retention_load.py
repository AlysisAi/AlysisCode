"""Opt-in real PostgreSQL cleanup contention, with actual local file bytes.

The inventory is synthetic, populated through the real store API within its
limits. This measures database cleanup, not cloud/model or fleet throughput.
"""

import json
import os
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_bots_backup import databases  # noqa: F401

from alysis_code.bots.models import BotInput
from alysis_code.bots.postgres import Connection
from alysis_code.bots.retention import ObjectRetention


@pytest.mark.skipif(
    not os.environ.get("ALYSIS_BOTS_RETENTION_LOAD_RECEIPT"),
    reason="Explicit bounded inventory study requested",
)
def test_cleanup_inventory_contention(databases, monkeypatch):  # noqa: F811
    store = databases()
    receipt = {
        "passed": False,
        "kind": "Actual PostgreSQL/local immutable files; synthetic inventory, no models/cloud",
        "external_model_calls": 0,
        "cloud_calls": 0,
        "samples": [],
    }
    bots, saved = {}, []
    other = store.save_bot("interactive", BotInput(name="Other account", purpose="Write probe"))
    execute = Connection.execute
    started = threading.Event()
    cleanup_thread = [None]
    statement_times = defaultdict(lambda: [0, 0.0])

    def observed(db, sql, args=None):
        if threading.get_ident() != cleanup_thread[0]:
            return execute(db, sql, args)
        # The actual PostgresStore.connect has already acquired its write lock.
        started.set()
        before = time.monotonic()
        try:
            return execute(db, sql, args)
        finally:
            kind = " ".join(sql.split()[:3])
            statement_times[kind][0] += 1
            statement_times[kind][1] += time.monotonic() - before

    monkeypatch.setattr(Connection, "execute", observed)

    def seed(owner, target):
        group = bots.setdefault(owner, [])
        while len(group) * 100 < target:
            bot = store.save_bot(
                owner, BotInput(name=f"Library {len(group)}", purpose="Synthetic retention load")
            )
            group.append(bot["id"])
            for index in range(100):
                data = f"{owner} Αθήνα 東京 {len(group)}:{index}".encode()
                file = store.save_bytes(owner, bot["id"], f"資料 {index}.txt", data)
                saved.append((owner, bot["id"], file["id"], data))

    def measure(label):
        for repeat in range(2):
            started.clear()
            statement_times.clear()

            def cleanup():
                cleanup_thread[0] = threading.get_ident()
                before = time.monotonic()
                try:
                    result = ObjectRetention(store, "library").sweep(limit=1)
                    return result, time.monotonic() - before
                finally:
                    cleanup_thread[0] = None

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(cleanup)
                assert started.wait(10)
                before = time.monotonic()
                store.save_bot(
                    "interactive",
                    BotInput(name="Other account", purpose=f"Write during {label}/{repeat}"),
                    bot_id=other["id"],
                )
                write_seconds = time.monotonic() - before
                result, sweep_seconds = future.result(timeout=30)
            assert result == {"deleted": 0, "pending": 0}
            sample = {
                "label": label,
                "repeat": repeat,
                "library_files": len(bots["library"]) * 100,
                "total_files": len(saved),
                "sweep_seconds": sweep_seconds,
                "other_account_write_seconds": write_seconds,
                "statements": dict(statement_times),
            }
            receipt["samples"].append(sample)
            print(json.dumps(sample), flush=True)

    try:
        for count in (100, 1000, 3000):
            seed("library", count)
            measure(f"{count} account files")
        seed("foreign", 3000)
        measure("3000 account plus 3000 foreign files")
        # Prove optimization must still adopt actual pre-inventory references.
        with store.connect(write=True) as db:
            legacy = db.execute(
                "SELECT key FROM stored_objects WHERE owner=? ORDER BY key LIMIT 3",
                ("library",),
            ).fetchall()
            for row in legacy:
                db.execute("DELETE FROM stored_objects WHERE key=?", (row[0],))
        measure("three legacy references")
        with store.connect() as db:
            assert db.execute("SELECT count(*) FROM stored_objects").fetchone()[0] == len(saved)
            assert (
                db.execute(
                    "SELECT count(*) FROM stored_objects WHERE state!='live' OR unreferenced_at IS NOT NULL"
                ).fetchone()[0]
                == 0
            )
        for owner, bot, file, data in saved:
            assert store.file_data(owner, bot, file) == data
        receipt["all_6000_file_bytes_preserved"] = True
        receipt["legacy_references_adopted"] = True
        receipt["passed"] = True
    finally:
        output = Path(os.environ["ALYSIS_BOTS_RETENTION_LOAD_RECEIPT"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(receipt, indent=2) + "\n")
