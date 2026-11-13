"""Conservative, restartable cleanup of registered bot objects only.

Publication and deletion are mutually exclusive database transitions. Network
calls happen outside transactions. Tombstones remain durable and are revisited
so a late upload after a lost response cannot resurrect an abandoned object.
Unknown bucket contents are never enumerated or deleted.
"""

import time
from dataclasses import dataclass

from ..cancellation import CooperativeCancellationError
from .object_storage import ObjectRef, ObjectStorageError
from .store import ACTIVE, Conflict


@dataclass(frozen=True)
class RetentionPolicy:
    grace_seconds: float = 86400
    workspace_age_seconds: float = 7 * 86400
    workspace_versions: int = 2
    tombstone_recheck_seconds: float = 86400

    def __post_init__(self):
        if (
            self.grace_seconds < 300
            or self.workspace_age_seconds < self.grace_seconds
            or self.workspace_versions < 2
            or self.tombstone_recheck_seconds < 300
        ):
            raise ValueError(
                "Retention must preserve two checkpoints and allow transfers to settle."
            )


def register_upload(store, owner, bot_id, ref):
    with store.connect(write=True) as db:
        store._bot(db, owner, bot_id)
        now = time.time()
        db.execute(
            "INSERT INTO stored_objects VALUES(?,?,?,?,'staged',?,?,?)",
            (ref.key, owner, bot_id, ref.encode(), now, now, now),
        )


def publish_object(db, owner, bot_id, ref):
    changed = db.execute(
        "UPDATE stored_objects SET state='live',unreferenced_at=NULL "
        "WHERE key=? AND owner=? AND bot_id=? AND ref=? AND state IN ('staged','live')",
        (ref.key, owner, bot_id, ref.encode()),
    ).rowcount
    if changed != 1:
        raise Conflict("Object upload expired or changed; retry the file operation.")


class ObjectRetention:
    def __init__(self, store, owner, policy=RetentionPolicy()):
        self.store, self.owner, self.policy = store, owner, policy

    def sweep(self, *, now=None, limit=100, cancel=lambda: False):
        """Bound remote deletions; inventory scans still scale with account size."""
        if cancel():
            raise CooperativeCancellationError("Object cleanup cancelled.")
        if self.store.objects is None:
            return {"deleted": 0, "pending": 0}
        now = time.time() if now is None else now
        if not 1 <= limit <= 1000:
            raise ValueError("Cleanup batch must be between 1 and 1000 objects.")
        with self.store.connect(write=True) as db:
            # Adopt references created before the inventory migration. This is
            # deliberately reference-driven, never a storage-prefix sweep.
            # Subtract every registered state, including tombstones. Stable
            # inventories need neither per-file inserts nor JSON decoding.
            references = db.execute(
                "SELECT f.ref,b.id,b.owner FROM file_objects f JOIN files l ON l.id=f.file_id "
                "JOIN bots b ON b.id=l.bot_id WHERE b.owner=? "
                "UNION SELECT w.ref,b.id,b.owner FROM workspace_objects w "
                "JOIN bots b ON b.id=w.bot_id WHERE b.owner=? "
                "UNION SELECT w.ref,b.id,b.owner FROM browser_states w "
                "JOIN bots b ON b.id=w.bot_id WHERE b.owner=? "
                "EXCEPT SELECT ref,bot_id,owner FROM stored_objects WHERE owner=?",
                (self.owner, self.owner, self.owner, self.owner),
            ).fetchall()
            for row in references:
                ref = ObjectRef.decode(row[0])
                db.execute(
                    "INSERT INTO stored_objects VALUES(?,?,?,?,'live',?,NULL,?) "
                    "ON CONFLICT(key) DO NOTHING",
                    (ref.key, row[2], row[1], ref.encode(), now, now),
                )
            self._prune_checkpoints(db, now)
            # An unreferenced live object gets a full grace interval after
            # discovery, rather than being deleted based on its original age.
            db.execute(
                "UPDATE stored_objects SET unreferenced_at=? "
                "WHERE owner=? AND state IN ('live','staged') AND unreferenced_at IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM file_objects f WHERE f.ref=stored_objects.ref) "
                "AND NOT EXISTS (SELECT 1 FROM workspace_objects w WHERE w.ref=stored_objects.ref) "
                "AND NOT EXISTS (SELECT 1 FROM browser_states w WHERE w.ref=stored_objects.ref)",
                (now, self.owner),
            )
            db.execute(
                "UPDATE stored_objects SET unreferenced_at=NULL WHERE owner=? AND state='live' "
                "AND unreferenced_at IS NOT NULL "
                "AND (EXISTS (SELECT 1 FROM file_objects f WHERE f.ref=stored_objects.ref) "
                "OR EXISTS (SELECT 1 FROM workspace_objects w WHERE w.ref=stored_objects.ref) "
                "OR EXISTS (SELECT 1 FROM browser_states w WHERE w.ref=stored_objects.ref))",
                (self.owner,),
            )
            rows = db.execute(
                "SELECT key,ref FROM stored_objects WHERE owner=? AND "
                "((state IN ('staged','live') AND unreferenced_at<=?) OR state='deleting' "
                "OR (state='deleted' AND checked_at<=?)) ORDER BY checked_at,key LIMIT ?",
                (
                    self.owner,
                    now - self.policy.grace_seconds,
                    now - self.policy.tombstone_recheck_seconds,
                    limit,
                ),
            ).fetchall()
            for row in rows:
                # Rotate attempted objects behind untouched work even if the
                # delete fails or this process exits before acknowledging it.
                # 'deleting' stays immediately eligible after a restart.
                db.execute(
                    "UPDATE stored_objects SET state='deleting',checked_at=? WHERE key=?",
                    (now, row[0]),
                )
        result = {"deleted": 0, "pending": 0}
        # Concurrent passes may select the same deleting key. Deletion is
        # idempotent; this bounded batch does not claim exclusive network work.
        for row in rows:
            if cancel():
                raise CooperativeCancellationError("Object cleanup cancelled.")
            try:
                self.store.objects.delete(ObjectRef.decode(row[1]))
            except ObjectStorageError:
                result["pending"] += 1
                continue
            with self.store.connect(write=True) as db:
                db.execute(
                    "UPDATE stored_objects SET state='deleted',checked_at=? WHERE key=? AND state='deleting'",
                    (now, row[0]),
                )
            result["deleted"] += 1
        return result

    def _prune_checkpoints(self, db, now):
        for table in ("workspace_objects", "browser_states"):
            self._prune_table(db, now, table)

    def _prune_table(self, db, now, table):
        rows = db.execute(
            f"SELECT w.id,w.bot_id,w.created_at,w.settled,t.status FROM {table} w "
            "JOIN bots b ON b.id=w.bot_id "
            "LEFT JOIN tasks t ON t.id=w.task_id WHERE b.owner=? "
            "ORDER BY w.bot_id,w.created_at DESC,w.id DESC",
            (self.owner,),
        ).fetchall()
        retained, obsolete = {}, []
        for row in rows:
            if row["status"] in ACTIVE and not row["settled"]:
                continue
            if row["settled"] or row["status"] in (None, "completed"):
                count = retained.get(row["bot_id"], 0)
                retained[row["bot_id"]] = count + 1
                if count < self.policy.workspace_versions:
                    continue
            if row["created_at"] <= now - self.policy.workspace_age_seconds:
                obsolete.append(row["id"])
        # Keep the same transaction/ranking but avoid one network round trip
        # per historical checkpoint. Stay below SQLite's conservative 999
        # bound-parameter limit; this does not bound the inventory scan.
        for start in range(0, len(obsolete), 500):
            batch = obsolete[start : start + 500]
            db.execute(
                f"DELETE FROM {table} WHERE id IN (" + ",".join("?" for _ in batch) + ")",
                batch,
            )
