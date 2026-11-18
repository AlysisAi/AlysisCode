"""Server job status comes from the worker's exit code."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from alysis_code.server.settings import ServerSettings
from alysis_code.server.store import ServerStore
from alysis_code.server.worker_runner import (
    STATUS_SOURCE_EXIT_CODE,
    JobRunner,
    JobState,
)


def _settings(tmp_path: Path) -> ServerSettings:
    return ServerSettings(
        host="127.0.0.1",
        port=7070,
        data_dir=tmp_path / "server-data",
        token=None,
        max_upload_bytes=2 * 1024 * 1024,
        max_concurrent_jobs=1,
        worker_backend="bwrap",
        worker_sandbox_mode="strict",
        worker_network="on",
        default_model="gpt-test",
        default_base_url=None,
        allow_client_base_url=False,
        allow_client_model=True,
    )


class _ScriptedPopen:
    def __init__(self, lines: list[str], *, exit_code: int) -> None:
        self.stdout = iter(lines)
        self._exit_code = exit_code

    def wait(self) -> int:
        return self._exit_code

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        return None


class _ScriptedRunner:
    def __init__(self, proc: _ScriptedPopen) -> None:
        self._proc = proc

    def spawn(self, **_kwargs: object) -> _ScriptedPopen:
        return self._proc


def _run_scripted_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    lines: list[str],
    exit_code: int,
) -> tuple[JobState, dict[str, object]]:
    settings = _settings(tmp_path)
    store = ServerStore(settings)
    run_id = store.create_empty_run()
    run_paths = store.get_run_paths(run_id)
    job_paths = store.create_job_paths(run_id, "job_test")
    state = JobState(
        job_id="job_test",
        run_id=run_id,
        status="queued",
        command=["alysis", "run", "do the thing"],
        created_at="2026-01-01T00:00:00+00:00",
        logs_path=os.fspath(job_paths.logs_path),
    )
    runner = JobRunner(settings, store)
    monkeypatch.setattr(
        runner,
        "_outer_runner",
        lambda: _ScriptedRunner(_ScriptedPopen(lines, exit_code=exit_code)),
    )
    try:
        runner._run_job_inner(state, run_paths, job_paths)
    finally:
        runner.close()
    result = json.loads(job_paths.result_path.read_text(encoding="utf-8"))
    return state, result


def test_zero_exit_code_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state, result = _run_scripted_job(
        tmp_path,
        monkeypatch,
        lines=["Wrote notes.md\n", "done\n"],
        exit_code=0,
    )

    assert state.status == "succeeded"
    assert state.status_source == STATUS_SOURCE_EXIT_CODE
    assert result["status"] == "succeeded"
    assert result["status_source"] == STATUS_SOURCE_EXIT_CODE


def test_nonzero_exit_code_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state, result = _run_scripted_job(
        tmp_path,
        monkeypatch,
        lines=["boom\n"],
        exit_code=3,
    )

    assert state.status == "failed"
    assert state.exit_code == 3
    assert state.status_source == STATUS_SOURCE_EXIT_CODE
    assert result["status"] == "failed"


def test_worker_output_that_looks_like_an_event_does_not_decide_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed_failure = json.dumps({"event": "run_completed", "data": {"ok": False}}) + "\n"
    state, _ = _run_scripted_job(
        tmp_path,
        monkeypatch,
        lines=[claimed_failure],
        exit_code=0,
    )

    assert state.status == "succeeded"
    assert state.status_source == STATUS_SOURCE_EXIT_CODE


def test_job_logs_still_contain_every_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_scripted_job(
        tmp_path,
        monkeypatch,
        lines=["progress line\n", "final line\n"],
        exit_code=0,
    )
    logs_path = next((tmp_path / "server-data").rglob("logs.txt"))
    logs = logs_path.read_text(encoding="utf-8")

    assert "progress line" in logs
    assert "final line" in logs
