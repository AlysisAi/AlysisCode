"""Durable, bot-scoped handoffs. Only the trusted coding worker may claim work."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints

from .models import Input
from .store import Conflict, LimitReached, NotFound, Store, encode, uid

ACTIVE = ("queued", "running")
WORKER_TIMEOUT = 15
# Compatibility fallback only for persisted jobs predating explicit snapshots.
# New jobs store None for autonomous work, or the configured study allowance.
EXECUTION_CALLS = 5
REPORT_CALLS = 1


class CodingAssignment(Input):
    brief: str = Field(min_length=1, max_length=6000)


class WorkspaceRegistration(Input):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    name: str = Field(min_length=1, max_length=100)
    worker_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class WorkspaceBinding(Input):
    workspace_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class WorkerRequest(Input):
    worker_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class CodingResult(WorkerRequest):
    instruction_revision: int = Field(default=0, ge=0)
    status: Literal["completed", "failed", "cancelled", "interrupted"]
    summary: str = Field(default="", max_length=16000)
    outcome: dict[str, Any] = Field(default_factory=dict)
    changes: Annotated[str, StringConstraints(strip_whitespace=False)] = Field(
        default="", max_length=48000
    )
    verification: dict[str, Any] = Field(default_factory=dict)
    files: list[str] = Field(default_factory=list, max_length=200)


class ReadCodingResult(Input):
    task_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class CodingProgress(WorkerRequest):
    name: str = Field(min_length=1, max_length=100)
    status: str = Field(min_length=1, max_length=80)


class CodingModelRequest(WorkerRequest):
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    phase: Literal["execute", "report"]
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = Field(default=None, max_length=100)
    max_tokens: int = Field(default=3000, ge=1)


class CodingStore:
    def __init__(self, store: Store, owner: str, *, execution_calls: int | None = None):
        if execution_calls is not None and (
            type(execution_calls) is not int or execution_calls < 1
        ):
            raise ValueError("Coding execution allowance must be positive when configured.")
        self.store, self.owner = store, owner
        self.execution_calls = execution_calls

    def workspaces(self) -> list[dict]:
        with self.store.connect() as db:
            return [
                {**dict(row), "online": time.time() - row["heartbeat"] < WORKER_TIMEOUT}
                for row in db.execute(
                    "SELECT * FROM coding_workspaces WHERE owner=? ORDER BY name,id", (self.owner,)
                )
            ]

    def register(self, data: WorkspaceRegistration, *, bot_id: str | None = None) -> dict:
        with self.store.connect(write=True) as db:
            if bot_id:
                self.store._bot(db, self.owner, bot_id)
                if data.id != bot_id:
                    raise Conflict("Managed workspace identity must match its bot.")
                bound = db.execute(
                    "SELECT id FROM coding_workspaces WHERE bot_id=?", (bot_id,)
                ).fetchone()
                if bound and bound[0] != data.id:
                    raise Conflict(
                        "This bot already has an explicit workspace; it cannot be replaced automatically."
                    )
            old = db.execute("SELECT * FROM coding_workspaces WHERE id=?", (data.id,)).fetchone()
            if old and old["owner"] != self.owner:
                raise NotFound("Workspace not found.")
            if old and old["worker_id"] != data.worker_id:
                if time.time() - old["heartbeat"] < WORKER_TIMEOUT:
                    raise Conflict("Another coding worker still owns this workspace.")
                rows = db.execute(
                    "SELECT * FROM coding_jobs WHERE workspace_id=? AND status IN ('queued','running')",
                    (data.id,),
                ).fetchall()
                for row in rows:
                    self._stop_job(
                        db,
                        row,
                        "interrupted",
                        "The coding worker stopped. Review the workspace before explicitly continuing.",
                    )
            db.execute(
                """INSERT INTO coding_workspaces VALUES(?,?,?,?,?,NULL,NULL)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,worker_id=excluded.worker_id,heartbeat=excluded.heartbeat""",
                (data.id, self.owner, data.name, data.worker_id, time.time()),
            )
            if bot_id:
                if not old or old["bot_id"] != bot_id:
                    db.execute(
                        "UPDATE action_proposals SET status='outdated',revision=revision+1 WHERE bot_id=? AND status='pending'",
                        (bot_id,),
                    )
                db.execute(
                    "UPDATE coding_workspaces SET bot_id=?,session_id=coalesce(session_id,?) WHERE id=?",
                    (bot_id, uid(), data.id),
                )
            return dict(
                db.execute("SELECT * FROM coding_workspaces WHERE id=?", (data.id,)).fetchone()
            )

    def managed_bots(self) -> list[dict]:
        with self.store.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT b.id,b.name FROM bots b LEFT JOIN coding_workspaces w ON w.bot_id=b.id WHERE b.owner=? AND (w.id IS NULL OR w.id=b.id) ORDER BY b.created_at,b.id",
                    (self.owner,),
                )
            ]

    def heartbeat(self, workspace_id: str, worker_id: str) -> None:
        with self.store.connect(write=True) as db:
            if not db.execute(
                "UPDATE coding_workspaces SET heartbeat=? WHERE id=? AND owner=? AND worker_id=?",
                (time.time(), workspace_id, self.owner, worker_id),
            ).rowcount:
                raise Conflict("This coding worker no longer owns the workspace.")

    def bind(self, bot_id: str, workspace_id: str | None) -> dict | None:
        with self.store.connect(write=True) as db:
            self.store._bot(db, self.owner, bot_id)
            if db.execute(
                "SELECT 1 FROM tasks WHERE bot_id=? AND status IN ('queued','running','waiting','cancelling')",
                (bot_id,),
            ).fetchone():
                raise Conflict("Stop this bot's work before changing its coding workspace.")
            current = db.execute(
                "SELECT * FROM coding_workspaces WHERE bot_id=?", (bot_id,)
            ).fetchone()
            if current and current["id"] == workspace_id:
                return dict(current)
            target = None
            if workspace_id:
                target = db.execute(
                    "SELECT * FROM coding_workspaces WHERE id=? AND owner=?",
                    (workspace_id, self.owner),
                ).fetchone()
                if not target:
                    raise NotFound("Coding workspace not found.")
                if target["bot_id"]:
                    raise Conflict("This workspace belongs to another bot.")
                if time.time() - target["heartbeat"] >= WORKER_TIMEOUT:
                    raise Conflict("The coding worker is offline.")
            db.execute(
                "UPDATE action_proposals SET status='outdated',revision=revision+1 WHERE bot_id=? AND status='pending'",
                (bot_id,),
            )
            if current:
                db.execute(
                    "UPDATE coding_workspaces SET bot_id=NULL,session_id=NULL WHERE id=?",
                    (current["id"],),
                )
            if target:
                db.execute(
                    "UPDATE coding_workspaces SET bot_id=?,session_id=? WHERE id=?",
                    (bot_id, uid(), workspace_id),
                )
                return dict(
                    db.execute(
                        "SELECT * FROM coding_workspaces WHERE id=?", (workspace_id,)
                    ).fetchone()
                )
            return None

    def recent(self, bot_id: str) -> list[dict]:
        with self.store.connect() as db:
            self.store._bot(db, self.owner, bot_id)
            rows = db.execute(
                "SELECT j.task_id,j.status,j.result FROM coding_jobs j JOIN tasks t ON t.id=j.task_id WHERE t.bot_id=? AND j.result IS NOT NULL ORDER BY j.created_at DESC LIMIT 3",
                (bot_id,),
            ).fetchall()
            return [
                {
                    "task_id": row["task_id"],
                    "status": row["status"],
                    "outcome": json.loads(row["result"]).get("outcome"),
                    "summary": json.loads(row["result"]).get("summary", "")[:2500],
                    "verification": json.loads(row["result"]).get("verification", {}),
                }
                for row in rows
            ]

    def active(self, bot_id: str) -> list[dict]:
        with self.store.connect() as db:
            self.store._bot(db, self.owner, bot_id)
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT j.id FROM coding_jobs j JOIN tasks t ON t.id=j.task_id WHERE t.bot_id=? AND j.status IN ('queued','running')",
                    (bot_id,),
                )
            ]
        return [
            {
                **{k: job[k] for k in ("id", "task_id", "status", "instruction_revision")},
                "request": job["context"]["original_request"],
            }
            for job in (self.get(i) for i in ids)
            if job["status"] in ACTIVE
        ]

    def instructions(self, job_id: str) -> list[dict]:
        with self.store.connect() as db:
            self._owned(db, job_id)
            return [
                dict(r)
                for r in db.execute(
                    "SELECT revision,source_task_id,content FROM coding_instructions WHERE job_id=? ORDER BY revision",
                    (job_id,),
                )
            ]

    def update_instructions(self, source_task: dict, task_id: str) -> dict:
        # The model selects the job; only the actual user's message becomes an instruction.
        with self.store.connect(write=True) as db:
            source = self.store._task(db, self.owner, source_task["id"])
            target = self.store._task(db, self.owner, task_id)
            if source["bot_id"] != target["bot_id"]:
                raise NotFound("Work not found for this bot.")
            if source["status"] != "running":
                raise Conflict("The new message is no longer being handled.")
            job = db.execute("SELECT * FROM coding_jobs WHERE task_id=?", (task_id,)).fetchone()
            if (
                not job
                or job["status"] not in ACTIVE
                or target["status"] not in ("running", "waiting")
            ):
                raise Conflict(
                    "That work has already stopped. The correction was not applied; inspect its result before continuing."
                )
            old = db.execute(
                "SELECT revision FROM coding_instructions WHERE job_id=? AND source_task_id=?",
                (job["id"], source["id"]),
            ).fetchone()
            if old:
                return {"task_id": task_id, "instruction_revision": old[0], "state": "queued"}
            revision = (
                db.execute(
                    "SELECT count(*) FROM coding_instructions WHERE job_id=?", (job["id"],)
                ).fetchone()[0]
                + 1
            )
            if revision > 16:
                raise LimitReached(
                    "This work has reached its correction allowance. Stop it before continuing with a new task."
                )
            db.execute(
                "INSERT INTO coding_instructions VALUES(?,?,?,?,?)",
                (job["id"], revision, source["id"], source["message"], time.time()),
            )
            self.store._event(
                db,
                task_id,
                "instructions_queued",
                {"revision": revision, "source_task_id": source["id"]},
            )
            return {
                "task_id": task_id,
                "instruction_revision": revision,
                "state": "queued",
                "message": "Saved for the next model step after the current action. This does not confirm it has been carried out.",
            }

    def read_result(self, bot_id: str, task_id: str) -> dict:
        with self.store.connect() as db:
            task = self.store._task(db, self.owner, task_id)
            if task["bot_id"] != bot_id:
                raise NotFound("Coding result not found for this bot.")
        job = self.for_task(task_id)
        if not job:
            raise NotFound("Coding result not found.")
        return job

    def workspace_for(self, bot_id: str) -> dict | None:
        return next((w for w in self.workspaces() if w["bot_id"] == bot_id), None)

    def submit(
        self, task: dict, brief: str, context: list[dict], *, continuation: dict | None = None
    ) -> dict:
        with self.store.connect(write=True) as db:
            parent = self.store._task(db, self.owner, task["id"])
            if parent["status"] != "running":
                raise Conflict("The parent task is no longer running.")
            old = db.execute(
                "SELECT * FROM coding_jobs WHERE task_id=?", (parent["id"],)
            ).fetchone()
            if old:
                return self._decode(old)
            workspace = db.execute(
                "SELECT * FROM coding_workspaces WHERE bot_id=? AND owner=?",
                (parent["bot_id"], self.owner),
            ).fetchone()
            if not workspace or time.time() - workspace["heartbeat"] >= WORKER_TIMEOUT:
                raise Conflict("Code execution is unavailable for this bot right now.")
            if (
                db.execute(
                    "SELECT 1 FROM coding_jobs WHERE workspace_id=? AND status IN ('queued','running')",
                    (workspace["id"],),
                ).fetchone()
                or db.execute(
                    "SELECT 1 FROM background_turns j JOIN tasks t ON t.id=j.task_id "
                    "WHERE t.bot_id=? AND t.status='waiting'",
                    (parent["bot_id"],),
                ).fetchone()
            ):
                raise Conflict(
                    "Work is already running. Read its state or update its instructions; do not start a second writer."
                )
            assignment = {
                "original_request": parent["message"],
                "brief": brief,
                # The worker quotes this conversation to its model as text, so
                # fields the host keeps for itself (underscore keys) stay out.
                "conversation": [
                    {k: v for k, v in message.items() if not k.startswith("_")}
                    for message in context
                ],
                # Trusted service policy, frozen when the job is created. Model
                # text and later service configuration cannot enlarge this job.
                "execution_calls": self.execution_calls,
            }
            job_id, now = uid(), time.time()
            db.execute(
                "INSERT INTO coding_jobs VALUES(?,?,?,?,NULL,'queued',?,NULL,?,?)",
                (
                    job_id,
                    parent["id"],
                    workspace["id"],
                    workspace["session_id"],
                    encode(assignment),
                    now,
                    now,
                ),
            )
            if continuation is not None:
                db.execute(
                    "INSERT INTO task_waits VALUES(?,?)", (parent["id"], encode(continuation))
                )
                db.execute(
                    "UPDATE tasks SET status='waiting',updated_at=? WHERE id=?", (now, parent["id"])
                )
            self.store._event(
                db,
                parent["id"],
                "coding_queued",
                {"job_id": job_id, "workspace": workspace["name"]},
            )
            return self._decode(
                db.execute("SELECT * FROM coding_jobs WHERE id=?", (job_id,)).fetchone()
            )

    @staticmethod
    def _decode(row) -> dict:
        return {
            **dict(row),
            "context": json.loads(row["context"]),
            "result": json.loads(row["result"]) if row["result"] else None,
        }

    def _owned(self, db, job_id: str):
        row = db.execute(
            "SELECT j.* FROM coding_jobs j JOIN tasks t ON t.id=j.task_id WHERE j.id=? AND t.owner=?",
            (job_id, self.owner),
        ).fetchone()
        if not row:
            raise NotFound("Coding task not found.")
        return row

    def _stop_job(self, db, row, status: str, reason: str) -> None:
        result = {
            "status": status,
            "summary": reason,
            "outcome": {
                "outcome": "cancelled" if status == "cancelled" else "incomplete",
                "verified_success": False,
            },
            "changes": "",
            "files": [],
            "verification": {},
        }
        db.execute(
            "UPDATE coding_jobs SET status=?,result=?,updated_at=? WHERE id=?",
            (status, encode(result), time.time(), row["id"]),
        )
        self.store._event(
            db, row["task_id"], "coding_" + status, {"job_id": row["id"], "message": reason}
        )

    def get(self, job_id: str) -> dict:
        with self.store.connect(write=True) as db:
            row = self._owned(db, job_id)
            if row["status"] in ACTIVE:
                parent = self.store._task(db, self.owner, row["task_id"])
                workspace = db.execute(
                    "SELECT * FROM coding_workspaces WHERE id=?", (row["workspace_id"],)
                ).fetchone()
                if parent["status"] not in ("running", "waiting"):
                    self._stop_job(
                        db,
                        row,
                        "cancelled"
                        if parent["status"] in ("cancelling", "cancelled")
                        else "interrupted",
                        "The parent task stopped; coding was not replayed.",
                    )
                elif (
                    time.time() - workspace["heartbeat"] >= WORKER_TIMEOUT
                    or time.time() - row["created_at"] > 600
                ):
                    self._stop_job(
                        db,
                        row,
                        "interrupted",
                        "The coding worker became unavailable. Inspect partial work before continuing.",
                    )
            result = self._decode(self._owned(db, job_id))
            result["instruction_revision"] = db.execute(
                "SELECT count(*) FROM coding_instructions WHERE job_id=?", (job_id,)
            ).fetchone()[0]
            return result

    def for_task(self, task_id: str) -> dict | None:
        with self.store.connect() as db:
            self.store._task(db, self.owner, task_id)
            row = db.execute("SELECT id FROM coding_jobs WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return None
        job = self.get(row[0])
        job.pop("context")
        return job

    def claim(self, workspace_id: str, worker_id: str) -> dict | None:
        from .hosted_maintenance import MaintenanceLease

        self.heartbeat(workspace_id, worker_id)
        with self.store.connect(write=True) as db:
            if MaintenanceLease.pending(db):
                return None
            if not db.execute(
                "SELECT 1 FROM coding_workspaces WHERE id=? AND owner=? AND worker_id=?",
                (workspace_id, self.owner, worker_id),
            ).fetchone():
                raise Conflict("The workspace belongs to another worker.")
            if db.execute(
                "SELECT 1 FROM coding_jobs WHERE workspace_id=? AND status='running'",
                (workspace_id,),
            ).fetchone():
                return None
            row = db.execute(
                "SELECT j.* FROM coding_jobs j JOIN tasks t ON t.id=j.task_id WHERE j.workspace_id=? AND j.status='queued' AND t.status IN ('running','waiting') ORDER BY j.created_at LIMIT 1",
                (workspace_id,),
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE coding_jobs SET status='running',worker_id=?,updated_at=? WHERE id=?",
                (worker_id, time.time(), row["id"]),
            )
            self.store._event(db, row["task_id"], "coding_running", {"job_id": row["id"]})
            return self._decode(self._owned(db, row["id"]))

    def require_worker(self, job_id: str, worker_id: str) -> dict:
        job = self.get(job_id)
        if job["worker_id"] != worker_id or job["status"] != "running":
            raise Conflict("Coding work is stopped or belongs to a different worker.")
        return job

    def finish(self, job_id: str, data: CodingResult) -> dict:
        with self.store.connect(write=True) as db:
            job = self._owned(db, job_id)
            if job["worker_id"] != data.worker_id:
                raise Conflict("This worker does not own the coding task.")
            result = data.model_dump(exclude={"worker_id"})
            result["receipt_recorded"] = True
            if len(encode(result)) > 90000:
                raise LimitReached("Coding result exceeds its allowance.")
            parent = self.store._task(db, self.owner, job["task_id"])
            if job["status"] in ACTIVE and parent["status"] not in ("running", "waiting"):
                self._stop_job(
                    db,
                    job,
                    "cancelled"
                    if parent["status"] in ("cancelled", "cancelling")
                    else "interrupted",
                    "The parent task stopped before the coding result arrived.",
                )
                job = self._owned(db, job_id)
            if job["status"] not in ACTIVE:
                previous = json.loads(job["result"]) if job["result"] else {}
                if previous.get("receipt_recorded"):
                    return self._decode(job)
                # Preserve the terminal verdict; a late worker may only attach
                # the real partial diff/checks collected during cancellation.
                previous.update(
                    {
                        key: result[key]
                        for key in ("changes", "files", "verification", "receipt_recorded")
                    }
                )
                db.execute(
                    "UPDATE coding_jobs SET result=?,updated_at=? WHERE id=?",
                    (encode(previous), time.time(), job_id),
                )
                self.store._event(db, job["task_id"], "coding_partial_results", {"job_id": job_id})
                return self._decode(self._owned(db, job_id))
            revision = db.execute(
                "SELECT count(*) FROM coding_instructions WHERE job_id=?", (job_id,)
            ).fetchone()[0]
            result["requested_instruction_revision"] = revision
            if data.instruction_revision != revision:
                result.update(
                    status="failed",
                    summary="A newer correction was not included in this result. Review partial work before continuing.",
                )
                result["outcome"] = {
                    "outcome": "incomplete",
                    "verified_success": False,
                    "reason": "newer_instructions_pending",
                }
            db.execute(
                "UPDATE coding_jobs SET status=?,result=?,updated_at=? WHERE id=?",
                (result["status"], encode(result), time.time(), job_id),
            )
            self.store._event(
                db,
                job["task_id"],
                "coding_" + result["status"],
                {"job_id": job_id, "outcome": result["outcome"].get("outcome", "incomplete")},
            )
            return self._decode(self._owned(db, job_id))

    def reserve_model(
        self, job_id: str, data: CodingModelRequest, model: str, max_calls: int | None
    ) -> tuple[str, dict | None]:
        self.require_worker(job_id, data.worker_id)
        if data.phase == "report" and data.tools:
            raise Conflict("The reporting phase cannot use tools.")
        request_hash = hashlib.sha256(
            json.dumps(
                data.model_dump(exclude={"worker_id", "request_id"}),
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        with self.store.connect(write=True) as db:
            job = self._owned(db, job_id)
            if job["status"] != "running" or job["worker_id"] != data.worker_id:
                raise Conflict("Coding task is no longer running.")
            previous = db.execute(
                "SELECT * FROM coding_calls WHERE job_id=? AND request_id=?",
                (job_id, data.request_id),
            ).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise Conflict("Model request identity was reused for different input.")
                if not previous["response"]:
                    raise Conflict(
                        "The earlier model attempt has no confirmed result. It will not be replayed."
                    )
                return previous["call_id"], json.loads(previous["response"])
            if db.execute(
                "SELECT 1 FROM coding_calls c JOIN provider_calls p ON p.id=c.call_id WHERE c.job_id=? AND p.status='started'",
                (job_id,),
            ).fetchone():
                raise Conflict("A coding model call is already in flight.")
            if (
                data.phase == "execute"
                and db.execute(
                    "SELECT 1 FROM coding_calls WHERE job_id=? AND phase='report'", (job_id,)
                ).fetchone()
            ):
                raise Conflict("The coding execution phase has ended.")
            used = db.execute(
                "SELECT count(*) FROM coding_calls WHERE job_id=? AND phase=?", (job_id, data.phase)
            ).fetchone()[0]
            execution_calls = json.loads(job["context"]).get("execution_calls", EXECUTION_CALLS)
            ceiling = execution_calls if data.phase == "execute" else REPORT_CALLS
            if ceiling is not None and used >= ceiling:
                raise LimitReached(
                    "The coding phase exhausted its model-call allowance. Report unfinished work."
                )
            call_id = self.store._reserve_call(
                db, self.owner, job["task_id"], model, max_calls, background=True
            )
            db.execute(
                "INSERT INTO coding_calls VALUES(?,?,?,?,?,NULL)",
                (job_id, data.request_id, data.phase, request_hash, call_id),
            )
            return call_id, None

    def finish_model(self, call_id: str, response: dict) -> None:
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE coding_calls SET response=? WHERE call_id=?", (encode(response), call_id)
            )
