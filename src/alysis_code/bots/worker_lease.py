"""Account-scoped durable worker leadership with monotonically fenced epochs.

An expired worker cannot renew or commit through its scoped store. Recovery
interrupts uncertain tasks rather than replaying them. One coordinator per
account is intentional; its foreground/background/peer workers still cooperate.
"""

import copy
import threading
from uuid import uuid4


class WorkerLeaseLost(RuntimeError):
    pass


class WorkerLease:
    def __init__(self, store, owner, *, ttl=60):
        if not store.shared or ttl < 15:
            raise ValueError("Shared worker leases require PostgreSQL and at least 15 seconds.")
        self.store, self.owner, self.ttl = store, owner, ttl
        self.worker_id, self.epoch = uuid4().hex, None
        self._closed = threading.Event()
        self._heartbeat = None

    @staticmethod
    def now(db):
        return db.execute(
            "SELECT extract(epoch FROM clock_timestamp())::double precision"
        ).fetchone()[0]

    def acquire(self):
        with self.store.connect(write=True) as db:
            now = self.now(db)
            old = db.execute("SELECT * FROM worker_leases WHERE owner=?", (self.owner,)).fetchone()
            if old is not None and old["expires"] > now:
                raise WorkerLeaseLost("Another Bots worker owns this account.")
            self.epoch = (old["epoch"] if old else 0) + 1
            db.execute(
                "INSERT INTO worker_leases VALUES(?,?,?,?) ON CONFLICT(owner) DO UPDATE "
                "SET worker_id=excluded.worker_id,epoch=excluded.epoch,expires=excluded.expires",
                (self.owner, self.worker_id, self.epoch, now + self.ttl),
            )
        scoped = copy.copy(self.store)
        scoped._fence = self
        return scoped

    def check(self, db):
        row = db.execute(
            "SELECT 1 FROM worker_leases WHERE owner=? AND worker_id=? AND epoch=? AND expires>?",
            (self.owner, self.worker_id, self.epoch, self.now(db)),
        ).fetchone()
        if row is None or self._closed.is_set():
            raise WorkerLeaseLost("Worker ownership expired; this result cannot be published.")

    def renew(self):
        with self.store.connect(write=True) as db:
            self.check(db)
            db.execute(
                "UPDATE worker_leases SET expires=? WHERE owner=? AND epoch=?",
                (self.now(db) + self.ttl, self.owner, self.epoch),
            )

    def start_heartbeat(self, lost):
        def loop():
            while not self._closed.wait(self.ttl / 4):
                try:
                    self.renew()
                except Exception:
                    lost()
                    return

        self._heartbeat = threading.Thread(target=loop, name="alysis-bots-lease", daemon=True)
        self._heartbeat.start()

    def close(self):
        # Release only our own epoch. A delayed close cannot release a successor.
        self._closed.set()
        if self._heartbeat:
            self._heartbeat.join(timeout=12)
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE worker_leases SET expires=0 WHERE owner=? AND worker_id=? AND epoch=?",
                (self.owner, self.worker_id, self.epoch),
            )
