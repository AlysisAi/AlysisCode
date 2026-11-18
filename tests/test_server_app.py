from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from alysis_code.server.app import (
    RunJobRequest,
    _agent_entrypoint_prefix,
    _append_common_agent_args,
    _build_run_job_command,
    _create_run_from_upload,
    create_app,
)
from alysis_code.server.settings import ServerSettings
from alysis_code.server.store import ServerStoreError


def _settings(*, worker_backend: str) -> ServerSettings:
    return ServerSettings(
        host="127.0.0.1",
        port=7070,
        data_dir=Path("/tmp/alysis-server-test"),
        token=None,
        max_upload_bytes=1024,
        max_concurrent_jobs=1,
        worker_backend=worker_backend,
        worker_sandbox_mode="strict",
        worker_network="on",
        default_model="gpt-test",
        default_base_url=None,
        allow_client_model=True,
        allow_client_base_url=False,
    )


class _BytesUpload:
    def __init__(self, payload: bytes, *, fail_on_unbounded_read: bool = False) -> None:
        self._payload = payload
        self._offset = 0
        self.fail_on_unbounded_read = fail_on_unbounded_read
        self.read_sizes: list[int] = []
        self.closed = False

    async def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if self.fail_on_unbounded_read and size < 0:
            raise AssertionError("expected bounded chunk reads")
        if self._offset >= len(self._payload):
            return b""
        if size < 0:
            end = len(self._payload)
        else:
            end = min(len(self._payload), self._offset + size)
        chunk = self._payload[self._offset : end]
        self._offset = end
        return chunk

    async def close(self) -> None:
        self.closed = True


class _CapturingStore:
    def __init__(self, *, result: str = "run_test") -> None:
        self.result = result
        self.called = False
        self.received_path: Path | None = None
        self.received_payload: bytes | None = None
        self.path_exists_during_call = False

    def create_run_from_zip_path(self, upload_path: Path) -> str:
        self.called = True
        self.received_path = upload_path
        self.path_exists_during_call = upload_path.exists()
        self.received_payload = upload_path.read_bytes()
        return self.result


async def _post_empty_run_with_client(
    *,
    app: object,
    client_addr: tuple[str, int],
    headers: dict[str, str] | None = None,
) -> object:
    httpx = pytest.importorskip("httpx")
    transport = httpx.ASGITransport(app=app, client=client_addr)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        return await client.post("/v1/runs/empty", headers=headers or {})


def test_append_common_agent_args_includes_requested_options() -> None:
    command = ["python", "-m", "alysis_code", "run"]
    _append_common_agent_args(
        command,
        mode="auto",
        yes=True,
        model="gpt-test",
        base_url="https://api.example.com/v1",
        temperature=0.2,
    )

    assert "--mode" in command
    assert "--yes" in command
    assert "--model" in command
    assert "--base-url" in command
    assert "--temperature" in command


def test_append_common_agent_args_skips_optional_when_missing() -> None:
    command = ["python", "-m", "alysis_code", "run"]
    _append_common_agent_args(
        command,
        mode="review",
        yes=False,
        model="gpt-test",
        base_url=None,
        temperature=None,
    )

    assert "--mode" in command
    assert "--yes" not in command
    assert "--base-url" not in command
    assert "--temperature" not in command


def test_agent_entrypoint_prefix_is_backend_aware() -> None:
    assert _agent_entrypoint_prefix(_settings(worker_backend="docker"))[0] == "python"
    assert _agent_entrypoint_prefix(_settings(worker_backend="bwrap"))[0] == sys.executable


def test_build_run_job_command_inserts_end_of_options_sentinel() -> None:
    req = RunJobRequest(
        instruction="--help",
        mode="auto",
        yes=True,
        model="ignored",
        base_url=None,
        temperature=None,
    )
    command = _build_run_job_command(
        settings=_settings(worker_backend="bwrap"),
        req=req,
        model="gpt-test",
        base_url=None,
    )
    assert command[-2] == "--"
    assert command[-1] == "--help"


def test_run_job_request_accepts_fullaccess_mode() -> None:
    req = RunJobRequest(instruction="do work", mode="fullaccess")
    assert req.mode == "fullaccess"


def test_build_run_job_command_preserves_fullaccess_mode_flag() -> None:
    req = RunJobRequest(
        instruction="implement feature",
        mode="fullaccess",
        yes=True,
        model="ignored",
        base_url=None,
        temperature=None,
    )
    command = _build_run_job_command(
        settings=_settings(worker_backend="bwrap"),
        req=req,
        model="gpt-test",
        base_url=None,
    )
    mode_idx = command.index("--mode")
    assert command[mode_idx + 1] == "fullaccess"


def test_create_app_smoke_openapi_generation(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    settings = ServerSettings(
        host="127.0.0.1",
        port=7070,
        data_dir=tmp_path / "server-data",
        token=None,
        max_upload_bytes=1024,
        max_concurrent_jobs=1,
        worker_backend="docker",
        worker_sandbox_mode="warn",
        worker_network="on",
        default_model="gpt-test",
        default_base_url=None,
        allow_client_model=True,
        allow_client_base_url=False,
    )
    app = create_app(settings)
    schema = app.openapi()
    assert "/health" in schema["paths"]


def test_create_app_token_auth_dependency_enforces_bearer_and_accepts_valid_token(
    tmp_path: Path,
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    settings = ServerSettings(
        host="127.0.0.1",
        port=7070,
        data_dir=tmp_path / "server-data-auth",
        token="secret-token",
        max_upload_bytes=1024,
        max_concurrent_jobs=1,
        worker_backend="docker",
        worker_sandbox_mode="warn",
        worker_network="on",
        default_model="gpt-test",
        default_base_url=None,
        allow_client_model=True,
        allow_client_base_url=False,
    )
    app = create_app(settings)
    client = TestClient(app)

    health_response = client.get("/health")
    assert health_response.status_code == 200
    assert health_response.json() == {"ok": True}

    missing_token_response = client.post("/v1/runs/empty")
    assert missing_token_response.status_code == 401
    assert missing_token_response.json()["detail"] == "Missing Bearer token."

    wrong_token_response = client.post(
        "/v1/runs/empty",
        headers={"authorization": "Bearer wrong-token"},
    )
    assert wrong_token_response.status_code == 403
    assert wrong_token_response.json()["detail"] == "Invalid token."

    ok_response = client.post(
        "/v1/runs/empty",
        headers={"authorization": "Bearer secret-token"},
    )
    assert ok_response.status_code == 200
    assert isinstance(ok_response.json().get("run_id"), str)


def test_create_app_fullaccess_run_job_route_preserves_mode_and_serves_no_forge_jobs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    captured_commands: list[list[str]] = []

    def _fake_start_job(self: object, *, run_id: str, command: list[str]) -> str:
        _ = run_id
        captured_commands.append(list(command))
        return f"job_test_{len(captured_commands)}"

    monkeypatch.setattr(
        "alysis_code.server.worker_runner.JobRunner.start_job",
        _fake_start_job,
    )

    settings = ServerSettings(
        host="127.0.0.1",
        port=7070,
        data_dir=tmp_path / "server-data-route-fullaccess",
        token="secret-token",
        max_upload_bytes=1024,
        max_concurrent_jobs=1,
        worker_backend="docker",
        worker_sandbox_mode="warn",
        worker_network="on",
        default_model="gpt-test",
        default_base_url=None,
        allow_client_model=True,
        allow_client_base_url=False,
    )
    app = create_app(settings)
    client = TestClient(app)
    headers = {"authorization": "Bearer secret-token"}

    run_resp = client.post("/v1/runs/empty", headers=headers)
    assert run_resp.status_code == 200
    run_id = run_resp.json()["run_id"]

    run_job_resp = client.post(
        f"/v1/runs/{run_id}/jobs/run",
        headers=headers,
        json={"instruction": "implement feature", "mode": "fullaccess"},
    )
    assert run_job_resp.status_code == 200
    assert "run" in captured_commands[-1]
    mode_idx = captured_commands[-1].index("--mode")
    assert captured_commands[-1][mode_idx + 1] == "fullaccess"

    started = len(captured_commands)
    for retired in ("forge_exec", "forge_swarm"):
        retired_resp = client.post(
            f"/v1/runs/{run_id}/jobs/{retired}",
            headers=headers,
            json={"task_id": "T01", "mode": "auto"},
        )
        assert retired_resp.status_code == 404
    assert len(captured_commands) == started


def test_create_app_auth_locality_policy_via_asgi_transport(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")

    settings = ServerSettings(
        host="127.0.0.1",
        port=7070,
        data_dir=tmp_path / "server-data-auth-locality",
        token=None,
        max_upload_bytes=1024,
        max_concurrent_jobs=1,
        worker_backend="docker",
        worker_sandbox_mode="warn",
        worker_network="on",
        default_model="gpt-test",
        default_base_url=None,
        allow_client_model=True,
        allow_client_base_url=False,
    )
    app = create_app(settings)

    localhost_resp = asyncio.run(
        _post_empty_run_with_client(
            app=app,
            client_addr=("127.0.0.1", 12345),
        )
    )
    assert localhost_resp.status_code == 200

    non_local_resp = asyncio.run(
        _post_empty_run_with_client(
            app=app,
            client_addr=("10.0.0.2", 12345),
        )
    )
    assert non_local_resp.status_code == 403
    assert (
        non_local_resp.json()["detail"]
        == "Server token is not set; only localhost clients are allowed."
    )


def test_create_app_auth_token_policy_via_asgi_transport(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")

    settings = ServerSettings(
        host="127.0.0.1",
        port=7070,
        data_dir=tmp_path / "server-data-auth-token",
        token="secret-token",
        max_upload_bytes=1024,
        max_concurrent_jobs=1,
        worker_backend="docker",
        worker_sandbox_mode="warn",
        worker_network="on",
        default_model="gpt-test",
        default_base_url=None,
        allow_client_model=True,
        allow_client_base_url=False,
    )
    app = create_app(settings)

    missing_token_resp = asyncio.run(
        _post_empty_run_with_client(
            app=app,
            client_addr=("10.0.0.2", 12345),
        )
    )
    assert missing_token_resp.status_code == 401
    assert missing_token_resp.json()["detail"] == "Missing Bearer token."

    wrong_token_resp = asyncio.run(
        _post_empty_run_with_client(
            app=app,
            client_addr=("10.0.0.2", 12345),
            headers={"authorization": "Bearer wrong-token"},
        )
    )
    assert wrong_token_resp.status_code == 403
    assert wrong_token_resp.json()["detail"] == "Invalid token."

    correct_token_resp = asyncio.run(
        _post_empty_run_with_client(
            app=app,
            client_addr=("10.0.0.2", 12345),
            headers={"authorization": "Bearer secret-token"},
        )
    )
    assert correct_token_resp.status_code == 200


def test_create_run_from_upload_stages_small_upload_and_passes_temp_path() -> None:
    upload = _BytesUpload(b"PK\x03\x04payload", fail_on_unbounded_read=True)
    store = _CapturingStore(result="run_small")

    run_id = asyncio.run(
        _create_run_from_upload(
            store=store,
            upload=upload,
            max_upload_bytes=1024,
            chunk_size=4,
        )
    )

    assert run_id == "run_small"
    assert store.called is True
    assert store.received_path is not None
    assert store.path_exists_during_call is True
    assert store.received_payload == b"PK\x03\x04payload"
    assert store.received_path.exists() is False
    assert upload.closed is True
    assert upload.read_sizes == [4, 4, 4, 4]


def test_create_run_from_upload_rejects_oversized_upload_before_store_call() -> None:
    upload = _BytesUpload(b"0123456789", fail_on_unbounded_read=True)
    store = _CapturingStore()

    with pytest.raises(ServerStoreError, match="Upload too large"):
        asyncio.run(
            _create_run_from_upload(
                store=store,
                upload=upload,
                max_upload_bytes=6,
                chunk_size=4,
            )
        )

    assert store.called is False
    assert upload.closed is True
    assert upload.read_sizes == [4, 4]
