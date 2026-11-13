"""Shared allocation intent. Local scratch is never evidence of cloud ownership."""

import json
import time

from .compute import ComputerIdentity, ComputerRef, wait_for
from .desktop_linux import DesktopCapacityBusy, DesktopError


class AllocationLedger:
    def __init__(self, store, owner):
        if not store.shared or store._fence is None:
            raise RuntimeError("Shared allocations require a fenced worker store.")
        self.store, self.owner = store, owner

    def get(self, bot_id):
        with self.store.connect() as db:
            self.store._bot(db, self.owner, bot_id)
            row = db.execute(
                "SELECT record FROM computer_allocations WHERE owner=? AND bot_id=?",
                (self.owner, bot_id),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def save(self, bot_id, value):
        with self.store.connect(write=True) as db:
            self.store._bot(db, self.owner, bot_id)
            db.execute(
                "INSERT INTO computer_allocations VALUES(?,?,?,?) "
                "ON CONFLICT(owner,bot_id) DO UPDATE SET record=excluded.record,updated_at=excluded.updated_at",
                (self.owner, bot_id, json.dumps(value), time.time()),
            )

    def reserve(self, bot_id, value, *, max_active):
        """Atomically reserve capacity before a provider can create a computer.

        Shared write locking covers every account. Unknown/creating/stopped
        allocations still occupy a slot; only confirmed deletion releases it.
        Policy disagreement fails closed instead of silently raising a budget.
        """
        if max_active is not None and (type(max_active) is not int or max_active < 1):
            raise ValueError("Computer capacity must be positive or explicit null.")
        if value.get("state") != "creating" or not value.get("scope"):
            raise ValueError("Reserve an explicit new allocation intent.")
        with self.store.connect(write=True) as db:
            self.store._bot(db, self.owner, bot_id)
            from .hosted_maintenance import MaintenanceLease

            if MaintenanceLease.pending(db):
                raise DesktopError("Cloud maintenance is in progress. Try again shortly.")
            policy = db.execute(
                "SELECT max_active FROM computer_capacity WHERE scope=?", (value["scope"],)
            ).fetchone()
            if policy is None:
                db.execute(
                    "INSERT INTO computer_capacity VALUES(?,?)", (value["scope"], max_active)
                )
            elif policy[0] != max_active:
                raise DesktopError(
                    "Computer capacity configuration differs; operator review is required."
                )
            previous = db.execute(
                "SELECT record FROM computer_allocations WHERE owner=? AND bot_id=?",
                (self.owner, bot_id),
            ).fetchone()
            if previous is not None and json.loads(previous[0])["state"] != "deleted":
                raise DesktopError("Previous computer cleanup is still pending.")
            count = db.execute(
                "SELECT count(*) FROM computer_allocations "
                "WHERE record::jsonb->>'scope'=? "
                "AND COALESCE(record::jsonb->>'state','') NOT IN ('deleted','sleeping','sleep_lost')",
                (value["scope"],),
            ).fetchone()[0]
            if max_active is not None and count >= max_active:
                raise DesktopCapacityBusy(
                    "All available computers are busy. Try again after current work finishes."
                )
            db.execute(
                "INSERT INTO computer_allocations VALUES(?,?,?,?) "
                "ON CONFLICT(owner,bot_id) DO UPDATE SET record=excluded.record,updated_at=excluded.updated_at",
                (self.owner, bot_id, json.dumps(value), time.time()),
            )

    def capacity(self, scope):
        with self.store.connect() as db:
            count = db.execute(
                "SELECT count(*) FROM computer_allocations WHERE record::jsonb->>'scope'=? "
                "AND COALESCE(record::jsonb->>'state','') NOT IN ('deleted','sleeping','sleep_lost')",
                (scope,),
            ).fetchone()[0]
            return count

    def activate(self, bot_id, allocation_id, *, max_active):
        """Reserve an existing sleeping allocation before any paid resume."""
        with self.store.connect(write=True) as db:
            self.store._bot(db, self.owner, bot_id)
            from .hosted_maintenance import MaintenanceLease

            if MaintenanceLease.pending(db):
                raise DesktopError("Cloud maintenance is in progress. Try again shortly.")
            row = db.execute(
                "SELECT record FROM computer_allocations WHERE owner=? AND bot_id=?",
                (self.owner, bot_id),
            ).fetchone()
            record = json.loads(row[0]) if row else None
            if (
                not record
                or record["allocation_id"] != allocation_id
                or record.get("resume_kind") != "memory"
            ):
                raise DesktopError("Sleeping computer ownership changed.")
            policy = db.execute(
                "SELECT max_active FROM computer_capacity WHERE scope=?", (record["scope"],)
            ).fetchone()
            if policy is None or policy[0] != max_active:
                raise DesktopError(
                    "Computer capacity configuration differs; operator review is required."
                )
            if record["state"] in {"sleeping", "sleep_lost"}:
                count = db.execute(
                    "SELECT count(*) FROM computer_allocations WHERE record::jsonb->>'scope'=? AND COALESCE(record::jsonb->>'state','') NOT IN ('deleted','sleeping','sleep_lost')",
                    (record["scope"],),
                ).fetchone()[0]
                if max_active is not None and count >= max_active:
                    raise DesktopCapacityBusy(
                        "All available computers are busy. Try again after current work finishes."
                    )
            record["state"] = "resuming"
            db.execute(
                "UPDATE computer_allocations SET record=?,updated_at=? WHERE owner=? AND bot_id=?",
                (json.dumps(record), time.time(), self.owner, bot_id),
            )

    def pending(self, *, include_sleeping=True):
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT bot_id,record FROM computer_allocations WHERE owner=?", (self.owner,)
            ).fetchall()
            return [
                (row[0], json.loads(row[1]))
                for row in rows
                if json.loads(row[1])["state"] != "deleted"
                and (
                    include_sleeping
                    or json.loads(row[1])["state"] not in {"sleeping", "sleep_lost"}
                )
            ]

    def reconcile(self, provider, *, preserve_memory=False):
        for bot_id, record in self.pending():
            if record["scope"] != provider.scope:
                raise DesktopError(
                    "Previous allocation requires its original provider for cleanup."
                )
            identity = ComputerIdentity(self.owner, bot_id, record["allocation_id"])
            ref = (
                ComputerRef(record["provider"], record["scope"], record["resource_id"], identity)
                if record.get("resource_id")
                else provider.find(identity)
            )
            if preserve_memory and record.get("resume_kind") == "memory":
                with self.store.connect() as db:
                    paused = db.execute(
                        "SELECT 1 FROM bot_computer_control WHERE bot_id=? AND state IN ('paused','viewing','suspending','idle')",
                        (bot_id,),
                    ).fetchone()
                if record["state"] in {"sleeping", "sleep_requested", "sleep_lost", "resuming"} or (
                    record["state"] == "allocated" and (record.get("task_id") is None or paused)
                ):
                    state = provider.status(ref).state if ref is not None else "missing"
                    if state == "running":
                        provider.stop(ref)
                        wait_for(provider, ref, "stopped", timeout=60)
                        state = "stopped"
                    if state not in {"stopped", "missing"}:
                        raise DesktopError("Sleeping computer state is not confirmed.")
                    self.save(
                        bot_id,
                        {**record, "state": "sleeping" if state == "stopped" else "sleep_lost"},
                    )
                    continue
            if ref is None:
                if record["state"] == "creating":
                    raise DesktopError(
                        "Earlier creation is uncertain; operator reconciliation required."
                    )
            else:
                state = provider.status(ref).state
                if state == "running":
                    provider.stop(ref)
                    wait_for(provider, ref, "stopped", timeout=60)
                    state = "stopped"
                if state == "stopped":
                    provider.delete(ref)
                    wait_for(provider, ref, "missing", timeout=60)
                elif state != "missing":
                    raise DesktopError("Previous computer cleanup remains unconfirmed.")
            self.save(bot_id, {**record, "state": "deleted"})
