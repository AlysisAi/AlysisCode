"""Account-authorized pairing; device credentials never authorize account APIs."""

from __future__ import annotations

import hashlib
import re
import time

from fastapi import HTTPException
from pydantic import Field

from .local_computers import DeviceConnection
from .store import Conflict, NotFound


class DevicePairing(DeviceConnection):
    # The native app generates and protects the credential. Only its digest is stored here.
    token_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class DeviceGrants:
    def __init__(self, store, owner, *, clock=time.time):
        self.store, self.owner, self.clock = store, owner, clock

    @staticmethod
    def digest(authorization):
        if not isinstance(authorization, str) or not re.fullmatch(
            r"Bearer device_[a-f0-9]{64}", authorization
        ):
            raise HTTPException(401, "Desktop pairing authorization required.")
        return hashlib.sha256(authorization[7:].encode()).hexdigest()

    def require(self, db, device_id, token_hash, *, receipt=False):
        row = db.execute(
            "SELECT * FROM bot_local_device_grants WHERE token_hash=? AND device_id=? AND owner=?",
            (token_hash, device_id, self.owner),
        ).fetchone()
        if not row or (row["revoked"] and not receipt):
            raise HTTPException(
                401, "Desktop access is unavailable. Pair this computer explicitly."
            )
        return row

    def pair(self, device_id, data):
        with self.store.connect(write=True) as db:
            device = db.execute(
                "SELECT owner FROM bot_local_devices WHERE id=?", (device_id,)
            ).fetchone()
            if device and device[0] != self.owner:
                raise NotFound("Computer not found.")
            previous = db.execute(
                "SELECT * FROM bot_local_device_grants WHERE token_hash=?", (data.token_hash,)
            ).fetchone()
            if previous:
                if (
                    previous["device_id"] != device_id
                    or previous["owner"] != self.owner
                    or previous["revoked"]
                ):
                    raise Conflict("Use a new device credential after revoking access.")
                return {"paired": True, "device_id": device_id, "owner": self.owner}
            if db.execute(
                "SELECT 1 FROM bot_local_device_grants WHERE device_id=? AND revoked=0",
                (device_id,),
            ).fetchone():
                raise Conflict(
                    "This computer is already paired. Revoke its access before pairing again."
                )
            db.execute(
                "INSERT INTO bot_local_devices VALUES(?,?,?,?,?,0,?) ON CONFLICT(id) DO UPDATE SET connected=0",
                (device_id, self.owner, data.name, data.platform, data.session_id, self.clock()),
            )
            db.execute(
                "INSERT INTO bot_local_device_grants VALUES(?,?,?,0,?)",
                (data.token_hash, device_id, self.owner, self.clock()),
            )
            return {"paired": True, "device_id": device_id, "owner": self.owner}

    def revoke(self, device_id):
        with self.store.connect(write=True) as db:
            if not db.execute(
                "SELECT 1 FROM bot_local_devices WHERE id=? AND owner=?", (device_id, self.owner)
            ).fetchone():
                raise NotFound("Computer not found.")
            db.execute(
                "UPDATE bot_local_device_grants SET revoked=1 WHERE device_id=? AND owner=?",
                (device_id, self.owner),
            )
            db.execute("UPDATE bot_local_devices SET connected=0 WHERE id=?", (device_id,))
            tasks = [
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT task_id FROM bot_local_requests WHERE device_id=? AND status IN ('pending','claimed')",
                    (device_id,),
                )
            ]
        # Claims check the grant in their write transaction, so none can pass this revocation.
        # Already admitted work is cancelled; its original credential may only deliver a receipt.
        for task_id in tasks:
            self.store.cancel(self.owner, task_id)
        return {"revoked": True}
