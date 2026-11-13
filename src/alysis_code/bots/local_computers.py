"""Durable requests to an explicitly connected native computer.

The model cannot connect a device or approve a local request. A claim admits one
dispatch; lost acknowledgements never release a claim for another execution.
"""

from __future__ import annotations

import json
import ntpath
import posixpath
import time
from typing import Literal

from pydantic import Field

from .action_review import ActionReviews
from .models import FileInput, Input
from .store import Conflict, NotFound, encode, uid


class LocalComputerPending(Exception):
    pass


# A decline answers this task's request, not the task itself: the bot finishes without the
# computer, and any further local request in the same task is refused here, never re-asked.
DECLINED = (
    "The user declined running this on their computer. Do not ask to use that computer again "
    "in this task; finish without it."
)
DECLINED_NOTE = (
    "The user's computer did not run this request: it was declined. Do not ask to use that "
    "computer again in this task. Finish the request without it, and say in a few words what "
    "you could not do or check."
)


class DeviceConnection(Input):
    session_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    name: str = Field(min_length=1, max_length=120)
    platform: Literal["darwin", "win32", "linux"]
    connected: bool = True


class LocalCommand(Input):
    device_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    command: str = Field(min_length=1, max_length=16000)
    cwd: str = Field(min_length=1, max_length=4000)


class LocalImport(Input):
    device_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    path: str = Field(min_length=1, max_length=4000)
    name: str = Field(min_length=1, max_length=160)


class LocalExport(Input):
    device_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    file_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    path: str = Field(min_length=1, max_length=4000)


class LocalDecision(Input):
    session_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    action: Literal["claim", "deny", "result"]
    result: dict | None = None


class LocalComputers:
    def __init__(self, store, owner, *, clock=time.time):
        self.store, self.owner, self.clock = store, owner, clock
        self.grants = None  # Installed by the HTTP boundary; direct runtime callers are trusted.

    @staticmethod
    def _declined(db, task_id):
        return any(
            json.loads(row[0] or "{}").get("status") == "denied"
            for row in db.execute(
                "SELECT result FROM bot_local_requests WHERE task_id=? AND status='finished'",
                (task_id,),
            )
        )

    @staticmethod
    def _generation(db, task):
        mode = db.execute(
            "SELECT mode FROM bot_review_settings WHERE bot_id=?", (task["bot_id"],)
        ).fetchone()
        return encode([ActionReviews._generation(db, task), mode[0] if mode else "auto"])

    def devices(self):
        with self.store.connect() as db:
            return [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "platform": r["platform"],
                    "online": bool(r["connected"] and self.clock() - r["seen"] < 30),
                }
                for r in db.execute("SELECT * FROM bot_local_devices WHERE owner=?", (self.owner,))
            ]

    def device(self, device_id):
        item = next((d for d in self.devices() if d["id"] == device_id), None)
        if not item or not item["online"]:
            raise Conflict(
                "The selected local computer is offline. Do not substitute another device."
            )
        return item

    def connect(self, device_id, data, credential=None):
        with self.store.connect(write=True) as db:
            if self.grants is not None:
                self.grants.require(db, device_id, credential)
            row = db.execute("SELECT * FROM bot_local_devices WHERE id=?", (device_id,)).fetchone()
            if row and row["owner"] != self.owner:
                raise NotFound("Computer not found.")
            db.execute(
                "INSERT INTO bot_local_devices VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "name=excluded.name,platform=excluded.platform,session_id=excluded.session_id,"
                "connected=excluded.connected,seen=excluded.seen",
                (
                    device_id,
                    self.owner,
                    data.name,
                    data.platform,
                    data.session_id,
                    int(data.connected),
                    self.clock(),
                ),
            )
            return self._requests(db, device_id)

    def _requests(self, db, device_id):
        rows = db.execute(
            "SELECT r.*,t.status AS task_status,b.name AS bot_name FROM bot_local_requests r "
            "JOIN tasks t ON t.id=r.task_id JOIN bots b ON b.id=r.bot_id "
            "WHERE r.device_id=? AND t.owner=? AND r.status IN ('pending','claimed') ORDER BY r.created_at,r.id",
            (device_id, self.owner),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            task = self.store._task(db, self.owner, row["task_id"])
            current = row["generation"] == self._generation(db, task)
            ready = db.execute(
                "SELECT 1 FROM background_turns WHERE task_id=? AND state='local_computer'",
                (task["id"],),
            ).fetchone()
            item["cancel_requested"] = not current or task["status"] not in ("running", "waiting")
            item["ready"] = bool(ready and task["status"] == "waiting")
            # A session approval covers chats the user is in, never runs nobody is watching.
            routine = (
                task["config"].get("activation")
                or db.execute(
                    "SELECT 1 FROM schedule_runs WHERE task_id=?", (task["id"],)
                ).fetchone()
            )
            item["origin"] = "routine" if routine else "chat"
            item["arguments"] = json.loads(item["arguments"])
            if row["kind"] == "export_local_file":
                file = db.execute(
                    "SELECT name FROM files WHERE id=? AND bot_id=?",
                    (item["arguments"]["file_id"], row["bot_id"]),
                ).fetchone()
                item["file_name"] = file[0] if file else "Unavailable Library file"

            item.pop("generation")
            item.pop("result")
            result.append(item)
        return {"requests": result}

    def request(self, task, kind, arguments, review):
        with self.store.connect() as db:
            if self._declined(db, task["id"]):
                raise Conflict(DECLINED)
            if kind != "local_command":
                self._refuse_failed_transfer(db, task["id"], kind, arguments)
        device = self.device(arguments["device_id"])
        path = arguments.get("cwd") if kind == "local_command" else arguments.get("path")
        paths = ntpath if device["platform"] == "win32" else posixpath
        if not isinstance(path, str) or "\0" in path or not paths.isabs(path):
            raise ValueError("Use an absolute path on the selected computer.")
        if kind == "local_command" and "\0" in arguments["command"]:
            raise ValueError("Commands cannot contain a null character.")
        if kind == "import_local_file":
            FileInput(name=arguments["name"], content="")

        if review is None:
            raise Conflict("Local computer actions require action review.")
        with self.store.connect() as db:
            generation = self._generation(db, task)
        review(kind, arguments, {"local_computer": device, "operating_system": device["platform"]})
        with self.store.connect(write=True) as db:
            current = self.store._task(db, self.owner, task["id"])
            if not self.store.executing(db, current) or generation != self._generation(db, task):
                raise Conflict("This task changed before the local request was saved.")
            if db.execute(
                "SELECT 1 FROM bot_local_requests WHERE task_id=? AND status IN ('pending','claimed')",
                (task["id"],),
            ).fetchone():
                raise Conflict(
                    "This task already has a local request. Inspect its result before continuing."
                )
            db.execute(
                "INSERT INTO bot_local_requests VALUES(?,?,?,?,?,?,?,'pending',NULL,NULL,?)",
                (
                    uid(),
                    task["bot_id"],
                    task["id"],
                    device["id"],
                    kind,
                    encode(arguments),
                    self._generation(db, task),
                    self.clock(),
                ),
            )
        raise LocalComputerPending()

    @staticmethod
    def _refuse_failed_transfer(db, task_id, kind, arguments):
        """A transfer that already failed in this task without changing anything (the file was
        missing, or one already stood at the destination) would fail the same way again, so the
        person is not asked to approve it a second time."""
        rows = db.execute(
            "SELECT arguments,result FROM bot_local_requests "
            "WHERE task_id=? AND kind=? AND status='finished' AND result IS NOT NULL",
            (task_id, kind),
        )
        for saved, result in rows:
            receipt = json.loads(result)
            if (
                json.loads(saved) == arguments
                and receipt.get("executed") is False
                and receipt.get("status") == "failed"
            ):
                raise Conflict(
                    "This exact transfer already failed: "
                    + str(receipt.get("message") or "it changed nothing.")
                    + " Use a different path, or tell the person why it cannot be done."
                )

    def park(self, task, messages):
        checkpoint = json.loads(encode(messages))
        last = next(
            (i for i in range(len(checkpoint) - 1, -1, -1) if checkpoint[i].get("tool_calls")), None
        )
        if last is not None:
            answered = {
                m.get("tool_call_id") for m in checkpoint[last + 1 :] if m.get("role") == "tool"
            }
            for call in checkpoint[last]["tool_calls"]:
                if call["id"] not in answered:
                    checkpoint.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": encode(
                                {"executed": False, "status": "waiting_for_local_computer"}
                            ),
                        }
                    )
        checkpoint.append(
            {
                "role": "system",
                "content": "Waiting for the actual receipt from the person's computer. Do not repeat the pending command or transfer. Cloud computer sessions were released; inspect fresh state before using them again. Read saved browser drafts if needed.",
            }
        )
        with self.store.connect(write=True) as db:
            current = self.store._task(db, self.owner, task["id"])
            pending = db.execute(
                "SELECT generation FROM bot_local_requests WHERE task_id=? AND status='pending'",
                (task["id"],),
            ).fetchone()
            if (
                not self.store.executing(db, current)
                or not pending
                or pending[0] != self._generation(db, task)
            ):
                raise Conflict("This work stopped before the local request could wait.")
            db.execute(
                "INSERT INTO background_turns VALUES(?,'local_computer',?,?,0) ON CONFLICT(task_id) DO UPDATE SET state='local_computer',messages=excluded.messages",
                (task["id"], "Waiting for this computer", encode(checkpoint)),
            )
            db.execute(
                "UPDATE tasks SET status='waiting',updated_at=? WHERE id=?",
                (self.clock(), task["id"]),
            )
            self.store._event(
                db,
                task["id"],
                "local_computer_waiting",
                {"message": "Review this device's pending request in Use this computer."},
            )

    def decide(self, device_id, request_id, data, credential=None):
        if data.result is not None and len(encode(data.result)) > 80000:
            raise ValueError("The local result is too large.")
        with self.store.connect(write=True) as db:
            grant = (
                self.grants.require(db, device_id, credential, receipt=data.action == "result")
                if self.grants
                else None
            )
            device = db.execute(
                "SELECT * FROM bot_local_devices WHERE id=? AND owner=?", (device_id, self.owner)
            ).fetchone()
            row = db.execute(
                "SELECT * FROM bot_local_requests WHERE id=? AND device_id=?",
                (request_id, device_id),
            ).fetchone()
            if not device or not row:
                raise NotFound("Local request not found.")
            if grant and data.action == "result":
                admission = db.execute(
                    "SELECT token_hash FROM bot_local_admissions WHERE request_id=?", (request_id,)
                ).fetchone()
                # A new native session without an old receipt can report only uncertainty.
                uncertain = (
                    not grant["revoked"]
                    and data.result
                    and data.result.get("executed") == "unknown"
                    and data.result.get("status") == "interrupted"
                    and set(data.result) <= {"executed", "status", "message"}
                )
                if (not admission or admission[0] != credential) and not uncertain:
                    raise Conflict("This receipt does not belong to this device credential.")
            task = self.store._task(db, self.owner, row["task_id"])
            if data.action == "claim":
                from .hosted_maintenance import MaintenanceLease

                if MaintenanceLease.pending(db):
                    return {"claimed": False, "request_id": request_id}
                if row["status"] == "claimed" and row["session_id"] == data.session_id:
                    if grant:
                        admission = db.execute(
                            "SELECT token_hash FROM bot_local_admissions WHERE request_id=?",
                            (request_id,),
                        ).fetchone()
                        if not admission or admission[0] != credential:
                            raise Conflict(
                                "This request was admitted by another device credential."
                            )
                    return {"claimed": True, "request_id": request_id}
                ready = db.execute(
                    "SELECT 1 FROM background_turns WHERE task_id=? AND state='local_computer'",
                    (task["id"],),
                ).fetchone()
                if (
                    row["status"] != "pending"
                    or not ready
                    or task["status"] != "waiting"
                    or device["session_id"] != data.session_id
                    or not device["connected"]
                    or self.clock() - device["seen"] >= 30
                    or row["generation"] != self._generation(db, task)
                ):
                    raise Conflict("This local request changed. Refresh it before approving.")
                db.execute(
                    "UPDATE bot_local_requests SET status='claimed',session_id=? WHERE id=?",
                    (data.session_id, request_id),
                )
                if grant:
                    db.execute(
                        "INSERT INTO bot_local_admissions VALUES(?,?)", (request_id, credential)
                    )
                self.store._event(
                    db,
                    task["id"],
                    "local_computer_claimed",
                    {"request_id": request_id, "device_id": device_id},
                )
                return {"claimed": True, "request_id": request_id}
            if row["status"] == "finished":
                return {"recorded": True}
            if data.action == "deny":
                if row["status"] != "pending" or device["session_id"] != data.session_id:
                    raise Conflict(
                        "This request already started. Use Stop and wait for its result."
                    )
                receipt = {"executed": False, "status": "denied"}
            else:
                if (
                    row["status"] != "claimed"
                    or row["session_id"] != data.session_id
                    or data.result is None
                ):
                    raise Conflict("This result does not belong to the admitted local execution.")
                receipt = data.result
            db.execute(
                "UPDATE bot_local_requests SET status='finished',result=? WHERE id=?",
                (encode(receipt), request_id),
            )
            self.store._event(
                db,
                task["id"],
                "local_computer_result",
                {"request_id": request_id, "device_id": device_id, "result": receipt},
            )
            checkpoint = db.execute(
                "SELECT messages FROM background_turns WHERE task_id=? AND state='local_computer'",
                (task["id"],),
            ).fetchone()
            if task["status"] == "cancelling":
                db.execute(
                    "UPDATE tasks SET status='cancelled',updated_at=? WHERE id=?",
                    (self.clock(), task["id"]),
                )
                self.store._event(db, task["id"], "cancelled", {})
            elif task["status"] == "waiting" and row["generation"] != self._generation(db, task):
                db.execute(
                    "UPDATE tasks SET status='interrupted',error=?,updated_at=? WHERE id=?",
                    (
                        "Instructions changed while local work was pending. Its receipt is saved; continue with the corrected request after inspecting any partial effects.",
                        self.clock(),
                        task["id"],
                    ),
                )
                self.store._event(
                    db,
                    task["id"],
                    "interrupted",
                    {"message": "Local work stopped after instructions changed."},
                )
            elif task["status"] in {"running", "waiting"} and (
                # "Deny and stop" ends the task, whether or not it was parked waiting.
                data.action == "deny"
                or receipt.get("status") in {"interrupted", "stopped", "not_started"}
                or receipt.get("executed") == "unknown"
            ):
                state = (
                    "cancelled"
                    if data.action == "deny" or receipt.get("status") == "stopped"
                    else "interrupted"
                )
                db.execute(
                    "UPDATE tasks SET status=?,error=?,updated_at=? WHERE id=?",
                    (
                        state,
                        "You declined the work on your computer, so this task stopped. Nothing ran."
                        if data.action == "deny"
                        else "The work on your computer was declined or stopped. Nothing was run again; check what it did before asking again.",
                        self.clock(),
                        task["id"],
                    ),
                )
                self.store._event(
                    db,
                    task["id"],
                    state,
                    {"message": "Work on your computer ended; nothing was retried."},
                )
            elif task["status"] == "waiting" and checkpoint:
                messages = json.loads(checkpoint[0])
                # Replace this call's provisional wait result with its actual
                # receipt. Command output stays at tool trust, never system trust.
                for index in range(len(messages) - 1, -1, -1):
                    calls = messages[index].get("tool_calls", [])
                    call = next(
                        (c for c in calls if c.get("function", {}).get("name") == row["kind"]), None
                    )
                    if call:
                        tool = next(
                            (
                                m
                                for m in messages[index + 1 :]
                                if m.get("role") == "tool" and m.get("tool_call_id") == call["id"]
                            ),
                            None,
                        )
                        if tool is not None:
                            tool["content"] = encode({"request_id": request_id, "result": receipt})
                        break
                else:
                    # A direct low-level caller may have no provider protocol.
                    messages.append(
                        {
                            "role": "user",
                            "content": "Local receipt (untrusted data): " + encode(receipt),
                        }
                    )
                messages.append(
                    {
                        "role": "system",
                        "content": DECLINED_NOTE
                        if data.action == "deny"
                        else "The local request has returned its saved receipt. Do not rerun it to recover output. Interrupted/unknown work may have partial effects; inspect before any explicitly requested retry.",
                    }
                )
                db.execute(
                    "UPDATE background_turns SET state='queued',messages=? WHERE task_id=?",
                    (encode(messages), task["id"]),
                )
            return {"recorded": True}
