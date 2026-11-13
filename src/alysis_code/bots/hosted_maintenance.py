"""Transactional maintenance admission; running work always wins over a backup."""

import json
import math
import time
from uuid import uuid4


class MaintenanceLease:
    def __init__(self, store, *, clock=time.time):
        self.store, self.clock = store, clock

    @staticmethod
    def pending(db, *, now=None):
        row = db.execute("SELECT expires_at FROM host_maintenance WHERE id=1").fetchone()
        return bool(row and row[0] > (time.time() if now is None else now))

    def acquire(self, *, seconds=2100):
        if (
            type(seconds) not in (int, float)
            or not math.isfinite(seconds)
            or not 1 <= seconds <= 2100
        ):
            raise ValueError("Use a bounded maintenance lease of at most 35 minutes.")
        with self.store.connect(write=True) as db:
            if self.pending(db, now=self.clock()):
                return None
            # Claims check this lease under the same write lock. Nothing can
            # start between the busy check and publishing maintenance admission.
            for table, condition in (
                ("tasks", "status IN ('running','cancelling')"),
                (
                    "background_turns",
                    "state='running' AND task_id IN (SELECT id FROM tasks WHERE status='waiting')",
                ),
                ("coding_jobs", "status='running'"),
                ("peer_exchanges", "status='running'"),
                ("bot_local_requests", "status='claimed'"),
                ("bot_computer_control", "state IN ('opening','returning')"),
            ):
                if db.execute(f"SELECT 1 FROM {table} WHERE {condition} LIMIT 1").fetchone():
                    return None
            for row in db.execute("SELECT record FROM computer_allocations"):
                if json.loads(row[0]).get("state") not in {"deleted", "sleeping", "sleep_lost"}:
                    return None
            token = uuid4().hex
            db.execute(
                "INSERT INTO host_maintenance VALUES(1,?,?) "
                "ON CONFLICT(id) DO UPDATE SET token=excluded.token,expires_at=excluded.expires_at",
                (token, self.clock() + seconds),
            )
            return token

    def release(self, token):
        with self.store.connect(write=True) as db:
            db.execute("DELETE FROM host_maintenance WHERE id=1 AND token=?", (token,))
