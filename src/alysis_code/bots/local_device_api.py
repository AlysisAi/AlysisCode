"""Only the admitted request's files and cancellation are available to a desktop."""

from __future__ import annotations

import asyncio
import json
from typing import Literal

from fastapi import HTTPException, Request
from pydantic import Field
from starlette.requests import ClientDisconnect

from .local_computers import DeviceConnection, LocalDecision
from .local_device_auth import DeviceGrants, DevicePairing
from .models import Input
from .store import Conflict, NotFound
from .transfers import UploadChunk, UploadInput


class TransferStep(Input):
    session_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    operation: Literal["begin", "append", "complete", "metadata", "download", "stop"]
    size: int | None = Field(default=None, ge=0, le=50 * 1024 * 1024, strict=True)
    offset: int | None = Field(default=None, ge=0, le=50 * 1024 * 1024, strict=True)
    data_base64: str | None = Field(default=None, max_length=350000)


def install_local_device_routes(app, runtime, transfers):
    local, store, owner = runtime.local_computers, runtime.store, runtime.owner
    grants = DeviceGrants(store, owner)
    local.grants = grants
    app.state.device_grants = grants

    async def validated(request, model):
        try:
            return model.model_validate(await request.json())
        except ClientDisconnect:
            raise HTTPException(
                400, "Desktop request connection closed before admission."
            ) from None
        except ValueError:
            raise HTTPException(422, "Invalid desktop request.") from None

    @app.post("/v1/local-computers/{device_id}/pair")
    async def pair(device_id: str, request: Request):
        data = await validated(request, DevicePairing)
        return await asyncio.to_thread(grants.pair, device_id, data)

    @app.post("/v1/local-computers/{device_id}/revoke")
    async def revoke(device_id: str):
        result = await asyncio.to_thread(grants.revoke, device_id)
        runtime.wake()
        return result

    @app.post("/v1/local-computers/{device_id}/connect")
    async def connect(device_id: str, request: Request):
        data = await validated(request, DeviceConnection)
        return await asyncio.to_thread(
            local.connect, device_id, data, request.state.device_credential
        )

    @app.post("/v1/local-computers/{device_id}/requests/{request_id}")
    async def decide(device_id: str, request_id: str, request: Request):
        data = await validated(request, LocalDecision)
        try:
            result = await asyncio.to_thread(
                local.decide, device_id, request_id, data, request.state.device_credential
            )
        except ValueError:
            raise HTTPException(422, "Invalid desktop result.") from None
        runtime.wake()
        return result

    def transfer(device_id, request_id, credential, data):
        def authorize(db):
            grants.require(db, device_id, credential)
            row = db.execute(
                "SELECT r.* FROM bot_local_requests r JOIN tasks t ON t.id=r.task_id "
                "WHERE r.id=? AND r.device_id=? AND t.owner=?",
                (request_id, device_id, owner),
            ).fetchone()
            if not row:
                raise NotFound("Local request not found.")
            if data.operation != "stop":
                admission = db.execute(
                    "SELECT token_hash FROM bot_local_admissions WHERE request_id=?", (request_id,)
                ).fetchone()
                task = store._task(db, owner, row["task_id"])
                if (
                    row["status"] != "claimed"
                    or row["session_id"] != data.session_id
                    or not admission
                    or admission[0] != credential
                    or task["status"] != "waiting"
                    or row["generation"] != local._generation(db, task)
                ):
                    raise Conflict("This transfer stopped or its permission changed.")
            return row

        with store.connect() as db:
            row = authorize(db)
            args = json.loads(row["arguments"])
            upload = db.execute(
                "SELECT id,file_id FROM file_uploads WHERE bot_id=? AND request_id=?",
                (row["bot_id"], request_id),
            ).fetchone()
        if data.operation == "stop":
            return store.cancel(owner, row["task_id"])
        bot_id = row["bot_id"]
        if row["kind"] == "import_local_file":
            if data.operation == "begin":
                return transfers.begin(
                    bot_id,
                    UploadInput(name=args["name"], size=data.size, request_id=request_id),
                    authorize=authorize,
                )
            if not upload:
                raise Conflict("Start this request's upload first.")
            if data.operation == "append":
                return transfers.append(
                    bot_id,
                    upload["id"],
                    UploadChunk(offset=data.offset, data_base64=data.data_base64),
                    authorize=authorize,
                )
            if data.operation == "complete":
                return transfers.complete(bot_id, upload["id"], authorize=authorize)
            if data.operation == "metadata" and upload["file_id"]:
                return store.file(owner, bot_id, upload["file_id"])
        elif (
            row["kind"] == "export_local_file"
            and data.operation == "download"
            and data.offset is not None
        ):
            return transfers.chunk(bot_id, args["file_id"], data.offset)
        raise Conflict("This operation is not part of the approved transfer.")

    @app.post("/v1/local-computers/{device_id}/requests/{request_id}/transfer")
    async def transfer_step(device_id: str, request_id: str, request: Request):
        data = await validated(request, TransferStep)
        try:
            result = await asyncio.to_thread(
                transfer, device_id, request_id, request.state.device_credential, data
            )
        except ValueError:
            raise HTTPException(422, "Invalid desktop transfer.") from None
        runtime.wake()
        return result
