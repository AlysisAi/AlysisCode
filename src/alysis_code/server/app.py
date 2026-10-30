import inspect
import sys
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ..branding import env_get
from .auth import require_token
from .job_config import resolve_effective_model_base_url
from .settings import ServerSettings
from .store import ServerStore, ServerStoreError
from .worker_runner import JobRunner

_UPLOAD_CHUNK_BYTES = 1024 * 1024
_AgentMode = Literal["auto", "review", "readonly", "fullaccess"]
_SwarmMode = Literal["auto"]


class RunJobRequest(BaseModel):
    instruction: str
    mode: _AgentMode = "auto"
    yes: bool = True
    model: str | None = None
    base_url: str | None = None
    temperature: float | None = None


class ForgeExecRequest(BaseModel):
    task_id: str = Field(min_length=1)
    mode: _AgentMode = "auto"
    yes: bool = True
    model: str | None = None
    base_url: str | None = None
    temperature: float | None = None


class ForgeSwarmRequest(BaseModel):
    # Non-dry-run swarm orchestration is auto-only by runtime invariant.
    mode: _SwarmMode = "auto"
    yes: bool = True
    model: str | None = None
    base_url: str | None = None
    temperature: float | None = None


async def _close_upload_best_effort(upload: object) -> None:
    close = getattr(upload, "close", None)
    if not callable(close):
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception:
        return


async def _stage_uploaded_zip(
    *,
    upload: object,
    max_upload_bytes: int,
    chunk_size: int = _UPLOAD_CHUNK_BYTES,
) -> Path:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be > 0")

    temp_path: Path | None = None
    total_bytes = 0

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as handle:
            temp_path = Path(handle.name)
            while True:
                chunk = await upload.read(chunk_size)  # type: ignore[attr-defined]
                if not chunk:
                    break
                next_total = total_bytes + len(chunk)
                if next_total > max_upload_bytes:
                    raise ServerStoreError(
                        f"Upload too large ({next_total} bytes); max is {max_upload_bytes} bytes."
                    )
                handle.write(chunk)
                total_bytes = next_total
        return temp_path
    except Exception:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise
    finally:
        await _close_upload_best_effort(upload)


async def _create_run_from_upload(
    *,
    store: ServerStore,
    upload: object,
    max_upload_bytes: int,
    chunk_size: int = _UPLOAD_CHUNK_BYTES,
) -> str:
    # Stage uploads to disk so size enforcement happens during request reads, not after
    # buffering the full multipart body into memory.
    staged_path = await _stage_uploaded_zip(
        upload=upload,
        max_upload_bytes=max_upload_bytes,
        chunk_size=chunk_size,
    )
    try:
        return store.create_run_from_zip_path(staged_path)
    finally:
        try:
            staged_path.unlink()
        except OSError:
            pass


def _append_common_agent_args(
    command: list[str],
    *,
    mode: str,
    yes: bool,
    model: str,
    base_url: str | None,
    temperature: float | None,
) -> None:
    command.extend(["--mode", mode, "--api-key-env", "ALYSIS_API_KEY"])
    if yes:
        command.append("--yes")
    command.extend(["--model", model])
    if base_url:
        command.extend(["--base-url", base_url])
    if temperature is not None:
        command.extend(["--temperature", str(temperature)])


def _agent_entrypoint_prefix(settings: ServerSettings) -> list[str]:
    if settings.worker_backend == "docker":
        return ["python", "-m", "alysis_code"]
    return [sys.executable, "-m", "alysis_code"]


def _append_forge_machine_flag(command: list[str], settings: ServerSettings) -> None:
    """Ask Forge workers for the NDJSON event stream so job status is reported, not guessed."""
    if settings.worker_machine_events:
        command.append("--machine")


def _validate_forge_exec_task_id(task_id: str) -> None:
    if task_id.startswith("-"):
        raise ValueError("Invalid task_id: must not start with '-'")
    if any(ch.isspace() for ch in task_id):
        raise ValueError("Invalid task_id: whitespace is not allowed")


def _build_run_job_command(
    *,
    settings: ServerSettings,
    req: RunJobRequest,
    model: str,
    base_url: str | None,
) -> list[str]:
    command = _agent_entrypoint_prefix(settings) + ["run", "--path", "/workspace"]
    _append_common_agent_args(
        command,
        mode=req.mode,
        yes=req.yes,
        model=model,
        base_url=base_url,
        temperature=req.temperature,
    )
    # End-of-options sentinel to prevent instruction option injection.
    command.extend(["--", req.instruction])
    return command


def _build_forge_exec_job_command(
    *,
    settings: ServerSettings,
    req: ForgeExecRequest,
    model: str,
    base_url: str | None,
) -> list[str]:
    _validate_forge_exec_task_id(req.task_id)
    command = _agent_entrypoint_prefix(settings) + [
        "forge",
        "exec",
        req.task_id,
        "--path",
        "/workspace",
    ]
    _append_forge_machine_flag(command, settings)
    _append_common_agent_args(
        command,
        mode=req.mode,
        yes=req.yes,
        model=model,
        base_url=base_url,
        temperature=req.temperature,
    )
    return command


def _build_forge_swarm_job_command(
    *,
    settings: ServerSettings,
    req: ForgeSwarmRequest,
    model: str,
    base_url: str | None,
) -> list[str]:
    command = _agent_entrypoint_prefix(settings) + [
        "forge",
        "swarm",
        "--path",
        "/workspace",
    ]
    _append_forge_machine_flag(command, settings)
    _append_common_agent_args(
        command,
        mode=req.mode,
        yes=req.yes,
        model=model,
        base_url=base_url,
        temperature=req.temperature,
    )
    return command


def create_app(settings: ServerSettings):  # type: ignore[no-untyped-def]
    from fastapi import Depends, FastAPI, File, HTTPException, UploadFile, status
    from fastapi.responses import PlainTextResponse

    app = FastAPI(title="alysis-code server", version="1.0")
    store = ServerStore(settings)
    runner = JobRunner(settings, store)
    auth_dep = require_token(settings)

    @app.get("/health")
    def health() -> dict[str, bool | str]:
        payload: dict[str, bool | str] = {"ok": True}
        if marker := env_get("ALYSIS_APP_SERVER_MARKER"):
            payload["instance_id"] = marker
        return payload

    @app.post("/v1/shutdown", dependencies=[Depends(auth_dep)])
    def shutdown() -> dict[str, bool | str]:
        runner.close()
        return {"ok": True, "workers": "stopped"}

    @app.post("/v1/runs", dependencies=[Depends(auth_dep)])
    async def create_run(file: UploadFile = File(...)) -> dict[str, str]:
        try:
            run_id = await _create_run_from_upload(
                store=store,
                upload=file,
                max_upload_bytes=settings.max_upload_bytes,
            )
        except ServerStoreError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
        return {"run_id": run_id}

    @app.post("/v1/runs/empty", dependencies=[Depends(auth_dep)])
    def create_empty_run() -> dict[str, str]:
        run_id = store.create_empty_run()
        return {"run_id": run_id}

    @app.post("/v1/runs/{run_id}/jobs/run", dependencies=[Depends(auth_dep)])
    def start_run_job(run_id: str, req: RunJobRequest) -> dict[str, str]:
        try:
            effective_model, effective_base_url = resolve_effective_model_base_url(
                settings=settings,
                requested_model=req.model,
                requested_base_url=req.base_url,
            )
            command = _build_run_job_command(
                settings=settings,
                req=req,
                model=effective_model,
                base_url=effective_base_url,
            )
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

        try:
            job_id = runner.start_job(run_id=run_id, command=command)
        except ServerStoreError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
        return {"job_id": job_id}

    @app.post("/v1/runs/{run_id}/jobs/forge_exec", dependencies=[Depends(auth_dep)])
    def start_forge_exec_job(run_id: str, req: ForgeExecRequest) -> dict[str, str]:
        try:
            effective_model, effective_base_url = resolve_effective_model_base_url(
                settings=settings,
                requested_model=req.model,
                requested_base_url=req.base_url,
            )
            command = _build_forge_exec_job_command(
                settings=settings,
                req=req,
                model=effective_model,
                base_url=effective_base_url,
            )
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

        try:
            job_id = runner.start_job(run_id=run_id, command=command)
        except ServerStoreError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
        return {"job_id": job_id}

    @app.post("/v1/runs/{run_id}/jobs/forge_swarm", dependencies=[Depends(auth_dep)])
    def start_forge_swarm_job(run_id: str, req: ForgeSwarmRequest) -> dict[str, str]:
        try:
            effective_model, effective_base_url = resolve_effective_model_base_url(
                settings=settings,
                requested_model=req.model,
                requested_base_url=req.base_url,
            )
            command = _build_forge_swarm_job_command(
                settings=settings,
                req=req,
                model=effective_model,
                base_url=effective_base_url,
            )
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

        try:
            job_id = runner.start_job(run_id=run_id, command=command)
        except ServerStoreError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
        return {"job_id": job_id}

    @app.get("/v1/jobs/{job_id}", dependencies=[Depends(auth_dep)])
    def get_job_status(job_id: str) -> dict[str, object]:
        try:
            status_obj = runner.get_status(job_id)
        except ServerStoreError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
        return {
            "job_id": status_obj.job_id,
            "run_id": status_obj.run_id,
            "status": status_obj.status,
            # How the status was decided: the worker's terminal event, or the exit code.
            "status_source": status_obj.status_source,
            "created_at": status_obj.created_at,
            "started_at": status_obj.started_at,
            "finished_at": status_obj.finished_at,
            "exit_code": status_obj.exit_code,
            "error": status_obj.error,
            "terminal_event": status_obj.terminal_event,
            "task_outcome": status_obj.task_outcome,
        }

    @app.get("/v1/jobs/{job_id}/logs", dependencies=[Depends(auth_dep)])
    def get_job_logs(job_id: str) -> PlainTextResponse:
        try:
            body = runner.read_logs(job_id)
        except ServerStoreError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
        return PlainTextResponse(body)

    @app.post("/v1/jobs/{job_id}/cancel", dependencies=[Depends(auth_dep)])
    def cancel_job(job_id: str) -> dict[str, str]:
        try:
            runner.cancel_job(job_id)
            status_obj = runner.get_status(job_id)
        except ServerStoreError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
        return {"job_id": status_obj.job_id, "status": status_obj.status}

    return app
