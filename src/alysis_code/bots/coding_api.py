"""Private worker transport. Provider credentials remain in the Linux controller."""

from __future__ import annotations

import json
from dataclasses import asdict

from fastapi import HTTPException

from ..cancellation import CooperativeCancellationError
from ..llm.base import require_tool_calling
from ..llm.types import LLMOutputLimitError
from .coding import (
    CodingModelRequest,
    CodingProgress,
    CodingResult,
    WorkerRequest,
    WorkspaceBinding,
    WorkspaceRegistration,
)


def install_coding_routes(app, *, runtime, settings, client_factory):
    coding = runtime.coding

    @app.get("/v1/coding/workspaces")
    def workspaces():
        return [
            {key: w[key] for key in ("id", "name", "bot_id", "online")} for w in coding.workspaces()
        ]

    @app.put("/v1/bots/{bot_id}/coding")
    def bind(bot_id: str, data: WorkspaceBinding):
        if not settings.coding_token:
            raise HTTPException(409, "The coding worker is not configured.")
        coding.bind(bot_id, data.workspace_id)
        return {"workspace_id": data.workspace_id}

    @app.post("/internal/coding/managed-bots")
    def managed_bots(data: WorkerRequest):
        if not settings.managed_workspaces:
            raise HTTPException(409, "Managed workspaces are not enabled.")
        return coding.managed_bots()

    @app.post("/internal/coding/managed-bots/{bot_id}/register")
    def register_managed(bot_id: str, data: WorkspaceRegistration):
        if not settings.managed_workspaces:
            raise HTTPException(409, "Managed workspaces are not enabled.")
        return coding.register(data, bot_id=bot_id)

    @app.post("/internal/coding/workspaces")
    def register(data: WorkspaceRegistration):
        return coding.register(data)

    @app.post("/internal/coding/workspaces/{workspace_id}/heartbeat")
    def heartbeat(workspace_id: str, data: WorkerRequest):
        coding.heartbeat(workspace_id, data.worker_id)
        return {"ok": True}

    @app.post("/internal/coding/workspaces/{workspace_id}/claim")
    def claim(workspace_id: str, data: WorkerRequest):
        return coding.claim(workspace_id, data.worker_id)

    @app.post("/internal/coding/jobs/{job_id}/status")
    def status(job_id: str, data: WorkerRequest):
        job = coding.get(job_id)
        if job["worker_id"] != data.worker_id:
            raise HTTPException(409, "This worker does not own the assignment.")
        return {"status": job["status"], "instruction_revision": job["instruction_revision"]}

    @app.post("/internal/coding/jobs/{job_id}/result")
    def finish(job_id: str, data: CodingResult):
        job = coding.finish(job_id, data)
        runtime.wake()
        return {"id": job["id"], "status": job["status"]}

    @app.post("/internal/coding/jobs/{job_id}/progress")
    def progress(job_id: str, data: CodingProgress):
        job = coding.require_worker(job_id, data.worker_id)
        runtime.store.event(
            settings.owner,
            job["task_id"],
            "coding_progress",
            {"name": data.name, "status": data.status},
        )
        return {"ok": True}

    @app.post("/internal/coding/jobs/{job_id}/model")
    def model(job_id: str, data: CodingModelRequest):
        job = coding.require_worker(job_id, data.worker_id)
        client = client_factory(job["task_id"])
        # Reserve a final parent-report call in the same durable allowance.
        call_id, saved = coding.reserve_model(
            job_id,
            data,
            client.model,
            None if settings.max_calls is None else settings.max_calls - 1,
        )
        if saved is not None:
            return saved
        instructions = coding.instructions(job_id)
        revision = instructions[-1]["revision"] if instructions else 0
        # The service owns memory receipts. Worker-supplied fields must not
        # impersonate service-side delivery evidence.
        messages = [
            {k: v for k, v in m.items() if not k.startswith("_memory")} for m in data.messages
        ]
        if instructions:
            messages.append(
                {
                    "role": "user",
                    "content": "Corrections to this current task, in order. These exact user messages supersede conflicting earlier requirements. Do not repeat completed actions unnecessarily.\n"
                    + json.dumps(instructions, ensure_ascii=False),
                }
            )
        usage_recorded = False
        try:
            task = runtime.store.task(settings.owner, job["task_id"])
            # Native worker responses belong to this authenticated assignment.
            # Prior parent replies are separately quoted handoff context.
            messages = [{**m, "_memory_turn_id": task["id"]} for m in messages]
            # Worker handoffs carry old context; refresh bot-owned memory at
            # each broker request, independently of the worker's local state.
            runtime._refresh_memory_context(task, messages, None)
            messages, memory_inputs = runtime._fit_context(
                task, messages, data.tools, with_memory_inputs=True
            )
            token = runtime.task_token(job["task_id"])
            token.throw_if_cancelled()
            if data.tools:
                require_tool_calling(client, [tool["function"]["name"] for tool in data.tools])
            limited = False
            try:
                response = client.chat(
                    messages=messages,
                    tools=data.tools,
                    max_tokens=min(data.max_tokens, runtime.limits.output_tokens),
                    cancellation_token=token,
                    stream=False,
                )
            except LLMOutputLimitError as exc:
                response, limited = exc.response, True
            runtime.store.finish_call(
                call_id,
                "incomplete" if limited else "completed",
                asdict(response.usage) if response.usage else None,
            )
            runtime._record_memory_delivery(task, memory_inputs, response)
            usage_recorded = True
            # Raw transport envelopes can contain headers. Transfer only normalized
            # response data required for continuation and usage, never raw responses.
            result = {
                "instruction_revision": revision,
                "content": response.content,
                "tool_calls": [asdict(call) for call in response.tool_calls],
                "usage": asdict(response.usage) if response.usage else None,
                "provider_metadata": response.provider_metadata,
                "response_model": response.response_model,
                "output_limit": limited,
            }
            coding.finish_model(call_id, result)
            if revision and data.phase == "execute":
                runtime.store.event(
                    settings.owner, job["task_id"], "instructions_received", {"revision": revision}
                )
            return result
        except CooperativeCancellationError:
            if not usage_recorded:
                runtime.store.finish_call(call_id, "unknown", None)
            raise HTTPException(
                409, "Coding was stopped; the model attempt was not replayed."
            ) from None
        except Exception:
            if not usage_recorded:
                runtime.store.finish_call(call_id, "unknown", None)
            raise HTTPException(
                502, "The coding model attempt failed. No automatic retry was made."
            ) from None
