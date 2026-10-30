from __future__ import annotations

import importlib
import json
import os
import socket
import stat
import threading
from pathlib import Path

import pytest
from rich.console import Console

import alysis_code.atomic_io as atomic_io_mod
import alysis_code.run_lock as run_lock_mod
from alysis_code.atomic_io import atomic_write_json, atomic_write_text
from alysis_code.config import AppConfig
from alysis_code.forge import create_plan_run
from alysis_code.run_lock import (
    RunMutationConflictError,
    acquire_run_mutation_guard,
    inspect_run_mutation_lock,
    lock_diagnostic,
    normalize_workspace_identity_path,
    workspace_mutation_run_id,
    write_run_mutation_lock_metadata,
)
from alysis_code.swarm_orchestrator import (
    MergeOutcome,
    _append_remote_report_update,
    _persist_worker_result,
    _write_merge_result,
    _write_swarm_summary,
    acquire_swarm_mutation_guard,
    run_swarm,
)
from alysis_code.swarm_worker import TaskWorkerResult


def test_atomic_io_imports_cleanly_from_fresh_checkout() -> None:
    atomic_io_mod = importlib.import_module("alysis_code.atomic_io")
    run_lock_import = importlib.import_module("alysis_code.run_lock")

    assert hasattr(atomic_io_mod, "atomic_write_text")
    assert hasattr(atomic_io_mod, "atomic_write_json")
    assert hasattr(run_lock_import, "acquire_run_mutation_guard")


def test_atomic_write_text_replaces_existing_file_without_temp_leaks(tmp_path: Path) -> None:
    target = tmp_path / "artifact.txt"
    target.write_text("before\n", encoding="utf-8")

    atomic_write_text(target, "after\n")

    assert target.read_text(encoding="utf-8") == "after\n"
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not portable to Windows")
def test_atomic_write_text_preserves_existing_file_mode(tmp_path: Path) -> None:
    target = tmp_path / "artifact.txt"
    target.write_text("before\n", encoding="utf-8")
    target.chmod(0o640)

    atomic_write_text(target, "after\n")

    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_atomic_write_json_overwrites_existing_file_atomically(tmp_path: Path) -> None:
    target = tmp_path / "artifact.json"
    target.write_text('{"before": true}\n', encoding="utf-8")

    atomic_write_json(target, {"after": True, "value": 3})

    assert target.read_text(encoding="utf-8") == '{\n  "after": true,\n  "value": 3\n}\n'
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []


def test_atomic_write_helpers_create_parent_dirs(tmp_path: Path) -> None:
    text_target = tmp_path / "nested" / "artifacts" / "artifact.txt"
    json_target = tmp_path / "nested" / "artifacts" / "artifact.json"

    atomic_write_text(text_target, "created\n")
    atomic_write_json(json_target, {"ok": True})

    assert text_target.read_text(encoding="utf-8") == "created\n"
    assert json_target.read_text(encoding="utf-8") == '{\n  "ok": true\n}\n'
    assert list(text_target.parent.glob(f".{text_target.name}.*.tmp")) == []
    assert list(json_target.parent.glob(f".{json_target.name}.*.tmp")) == []


def test_atomic_write_text_cleans_temp_file_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "artifact.txt"

    def fail_replace(_src: Path, _dst: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("alysis_code.atomic_io.os.replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        atomic_write_text(target, "after\n")

    assert target.exists() is False
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []


def test_atomic_write_text_preserves_newlines_exactly(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "artifact.txt"
    original_fdopen = atomic_io_mod.os.fdopen
    captured: dict[str, str | None] = {"newline": None}

    def recording_fdopen(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured["newline"] = kwargs.get("newline")
        return original_fdopen(*args, **kwargs)

    monkeypatch.setattr("alysis_code.atomic_io.os.fdopen", recording_fdopen)

    atomic_write_text(target, "line1\r\nline2\n")

    assert captured["newline"] == ""
    assert target.read_bytes() == b"line1\r\nline2\n"


def test_atomic_write_text_raises_on_real_directory_fsync_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "nt":
        pytest.skip("directory fsync is not attempted on Windows")

    target = tmp_path / "artifact.txt"
    real_open = atomic_io_mod.os.open
    real_fsync = atomic_io_mod.os.fsync
    sentinel_dir_fd = 999_999

    def open_wrapper(path, flags, mode=0o777):  # type: ignore[no-untyped-def]
        if Path(path) == tmp_path and flags == atomic_io_mod.os.O_RDONLY:
            return sentinel_dir_fd
        return real_open(path, flags, mode)

    def fsync_wrapper(fd: int) -> None:
        if fd == sentinel_dir_fd:
            raise OSError(5, "directory fsync failed")
        real_fsync(fd)

    monkeypatch.setattr("alysis_code.atomic_io.os.open", open_wrapper)
    monkeypatch.setattr("alysis_code.atomic_io.os.fsync", fsync_wrapper)
    monkeypatch.setattr("alysis_code.atomic_io.os.close", lambda _fd: None)

    with pytest.raises(OSError, match="directory fsync failed"):
        atomic_write_text(target, "after\n")

    assert target.read_text(encoding="utf-8") == "after\n"
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []


def test_swarm_artifact_writes_publish_atomically(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)

    worker_result = TaskWorkerResult(
        task_id="T01",
        title="Task A",
        branch="feat/t01-a",
        worktree_path=os.fspath(paths.run_dir / "worktrees" / "T01" / "repo"),
        started_at="2026-03-26T00:00:00+00:00",
        finished_at="2026-03-26T00:01:00+00:00",
        success=True,
        summary="ok",
        commit_hash="abc123",
        error=None,
        report_path=".alysis/runs/x/execution/reports/T01.md",
        patch_path=".alysis/runs/x/execution/patches/T01.diff",
        log_path=".alysis/runs/x/execution/logs/T01.jsonl",
        log_pointer_path=".alysis/runs/x/execution/logs/T01.log.json",
        warnings=[],
        changed_files=["src/example.py"],
        verify_failed=False,
        verify_summary="verification passed (1/1)",
        verify_artifact_path=".alysis/runs/x/execution/verify/T01.txt",
    )
    worker_result_path = _persist_worker_result(paths, worker_result)
    assert json.loads(worker_result_path.read_text(encoding="utf-8"))["task_id"] == "T01"
    assert list(worker_result_path.parent.glob(f".{worker_result_path.name}.*.tmp")) == []

    merge_result_path = _write_merge_result(
        paths,
        MergeOutcome(
            task_id="T01",
            branch="feat/t01-a",
            success=True,
            merge_commit_hash="def456",
            error=None,
            backend_name="snapshot_workspace",
            action="applied",
        ),
    )
    assert json.loads(merge_result_path.read_text(encoding="utf-8"))["action"] == "applied"
    assert list(merge_result_path.parent.glob(f".{merge_result_path.name}.*.tmp")) == []

    summary_path = _write_swarm_summary(
        paths=paths,
        backend_name="snapshot_workspace",
        base_branch="main",
        executed=["T01"],
        merge_outcomes=[],
        integration_results=[],
        replanning_results=[],
        skipped={},
        recovered={},
        startup_warnings=[],
        dry_run=False,
        schedule_preview=[["T01"]],
        workspace_summary_lines=["Workspace root: `repo`"],
        binding_summary_lines=["Requested Path: `repo`"],
    )
    summary_text = summary_path.read_text(encoding="utf-8")
    assert "# Swarm Summary" in summary_text
    assert "T01" in summary_text
    assert list(summary_path.parent.glob(f".{summary_path.name}.*.tmp")) == []

    report_path = repo / "report.md"
    report_path.write_text("# Task Report\n", encoding="utf-8")
    _append_remote_report_update(
        paths=paths,
        report_path_raw=os.fspath(report_path.relative_to(paths.root)),
        record={
            "provider": "github",
            "branch": "feat/t01-a",
            "branch_push_status": "pushed",
            "pr_status": "created",
            "pr_url": "https://example.invalid/pr/1",
        },
    )
    report_text = report_path.read_text(encoding="utf-8")
    assert "## Remote Sync Update" in report_text
    assert "provider=github" in report_text
    assert list(report_path.parent.glob(f".{report_path.name}.*.tmp")) == []


def test_run_swarm_setup_failure_preserves_caller_owned_lock(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    guard = acquire_swarm_mutation_guard(paths, mode="forge_swarm:test")

    import alysis_code.swarm_orchestrator as swarm_orchestrator_mod

    monkeypatch.setattr(
        swarm_orchestrator_mod,
        "ensure_swarm_dirs",
        lambda _paths: (_ for _ in ()).throw(RuntimeError("boom-after-lock")),
    )

    with pytest.raises(RuntimeError, match="boom-after-lock"):
        run_swarm(
            paths=paths,
            plan={"run_id": paths.run_id, "tasks": []},
            cfg=AppConfig(model="test-model"),
            mode="auto",
            yes=False,
            max_steps=1,
            api_key_override="k",
            no_log=True,
            parallel=1,
            base_branch=None,
            max_tasks=None,
            max_attempts=None,
            dry_run=False,
            keep_worktrees=False,
            retry_failed=False,
            only=None,
            retry_merge_conflicts=False,
            console=Console(),
            run_mutation_guard=guard,
        )

    metadata = inspect_run_mutation_lock(paths.run_dir)
    assert metadata is not None
    assert metadata["owner_token"] == guard.owner_token

    guard.release()
    assert not (paths.run_dir / "active_execution.lock.json").exists()


def test_run_mutation_guard_writes_structured_lock_metadata(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"

    with acquire_run_mutation_guard(
        run_id="run-1",
        mode="forge_swarm",
        run_dir=run_dir,
        workspace_root=tmp_path,
        owner_session_id="session-123",
    ) as guard:
        metadata = inspect_run_mutation_lock(run_dir)
        assert metadata is not None
        assert metadata["owner_id"] == "session-123"
        assert metadata["owner_session_id"] == "session-123"
        assert metadata["started_at"]
        assert metadata["last_heartbeat_at"]
        assert metadata["workspace_identity"] == normalize_workspace_identity_path(tmp_path)
        assert metadata["stale_policy"]
        diagnostic = lock_diagnostic(metadata, run_id="run-1", mode="forge_swarm")
        assert diagnostic["reason_code"] == "blocked_by_active_workspace_execution"
        assert "Forge execution" in diagnostic["diagnostic"]
        assert "owner_token" not in diagnostic["blocked_by"]

        guard.refresh_heartbeat()
        refreshed = inspect_run_mutation_lock(run_dir)
        assert refreshed is not None
        assert refreshed["last_heartbeat_at"]


def test_run_mutation_guard_waits_for_active_lock_and_records_queue(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    active_guard = acquire_run_mutation_guard(
        run_id="run-1",
        mode="forge_swarm:first",
        run_dir=run_dir,
        workspace_root=tmp_path,
    )
    queued_started = threading.Event()
    queued_result: dict[str, object] = {}

    def acquire_queued() -> None:
        try:
            queued_guard = acquire_run_mutation_guard(
                run_id="run-1",
                mode="forge_swarm:second",
                run_dir=run_dir,
                workspace_root=tmp_path,
                wait=True,
                wait_timeout_s=5,
                poll_interval_s=0.05,
                on_wait=lambda _info: queued_started.set(),
            )
            queued_result["guard"] = queued_guard
        except BaseException as exc:  # noqa: BLE001
            queued_result["exc"] = exc

    thread = threading.Thread(target=acquire_queued, daemon=True)
    thread.start()
    assert queued_started.wait(timeout=2)
    assert inspect_run_mutation_lock(run_dir)["owner_token"] == active_guard.owner_token

    active_guard.release()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert "exc" not in queued_result
    queued_guard = queued_result["guard"]
    assert queued_guard.acquired_after_wait is True
    assert queued_guard.wait_started_at
    assert queued_guard.wait_finished_at
    queued_guard.release()
    events_path = run_dir / "active_execution.events.jsonl"
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [event["event"] for event in events] == [
        "queued_wait_started",
        "queued_wait_finished",
    ]


def test_run_mutation_guard_wait_timeout_has_structured_reason(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"

    with acquire_run_mutation_guard(
        run_id="run-1",
        mode="forge_swarm:first",
        run_dir=run_dir,
        workspace_root=tmp_path,
    ):
        with pytest.raises(RunMutationConflictError) as excinfo:
            acquire_run_mutation_guard(
                run_id="run-1",
                mode="forge_swarm:second",
                run_dir=run_dir,
                workspace_root=tmp_path,
                wait=True,
                wait_timeout_s=0,
                poll_interval_s=0.05,
            )

    assert excinfo.value.reason_code == "active_execution_wait_timeout"
    assert excinfo.value.metadata is not None
    assert "owner_token" not in excinfo.value.metadata


def test_workspace_lock_identity_normalizes_cross_platform_path_forms(tmp_path: Path) -> None:
    assert normalize_workspace_identity_path("C:\\Users\\Public\\Repo\\") == (
        normalize_workspace_identity_path("c:/users/public/repo")
    )
    assert normalize_workspace_identity_path("/mnt/c/Users/Public/Repo/") == (
        normalize_workspace_identity_path("C:/Users/Public/Repo")
    )
    assert workspace_mutation_run_id("C:\\Users\\Public\\Repo").startswith(
        "workspace:c:/users/public/repo"
    )
    resolved_tmp_path = os.fspath(tmp_path.resolve()).replace("\\", "/")
    expected_tmp_identity = resolved_tmp_path
    if len(resolved_tmp_path) >= 3 and resolved_tmp_path[1:3] == ":/":
        expected_tmp_identity = resolved_tmp_path.casefold()
    elif resolved_tmp_path.startswith("/mnt/") and len(resolved_tmp_path) > 6:
        expected_tmp_identity = f"{resolved_tmp_path[5]}:{resolved_tmp_path[6:]}".lower()
    assert normalize_workspace_identity_path(tmp_path) == expected_tmp_identity


def test_swarm_mutation_guard_serializes_different_runs_in_same_workspace(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    first_paths = create_plan_run(repo)
    second_paths = create_plan_run(repo)
    first_guard = acquire_swarm_mutation_guard(first_paths, mode="forge_swarm:first")
    queued_started = threading.Event()
    queued_result: dict[str, object] = {}

    def acquire_second() -> None:
        try:
            queued_result["guard"] = acquire_swarm_mutation_guard(
                second_paths,
                mode="forge_swarm:second",
                wait=True,
                wait_timeout_s=5,
                poll_interval_s=0.05,
                on_wait=lambda _info: queued_started.set(),
            )
        except BaseException as exc:  # noqa: BLE001
            queued_result["exc"] = exc

    thread = threading.Thread(target=acquire_second, daemon=True)
    thread.start()
    assert queued_started.wait(timeout=2)
    assert not queued_result

    first_guard.release()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert "exc" not in queued_result
    second_guard = queued_result["guard"]
    assert second_guard.acquired_after_wait is True
    second_guard.release()


def test_run_mutation_guard_recovers_definitely_stale_lock(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    lock_path = run_dir / "active_execution.lock.json"
    write_run_mutation_lock_metadata(
        lock_path,
        {
            "schema_version": 1,
            "run_id": "run-1",
            "mode": "forge_swarm",
            "kind": "lock",
            "pid": 9_999_999,
            "hostname": socket.gethostname(),
            "acquired_at": "2026-03-26T00:00:00+00:00",
            "owner_token": "stale-owner",
            "workspace_root": os.fspath(tmp_path),
            "run_dir": os.fspath(run_dir),
        },
    )

    with acquire_run_mutation_guard(
        run_id="run-1",
        mode="forge_swarm",
        run_dir=run_dir,
        workspace_root=tmp_path,
    ):
        metadata = inspect_run_mutation_lock(run_dir)
        assert metadata is not None
        assert metadata["run_id"] == "run-1"
        assert metadata["mode"] == "forge_swarm"
        assert metadata["pid"] == os.getpid()
        assert metadata["hostname"] == socket.gethostname()

    assert not lock_path.exists()


def test_run_mutation_guard_fails_closed_for_ambiguous_or_active_lock(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    lock_path = run_dir / "active_execution.lock.json"
    write_run_mutation_lock_metadata(
        lock_path,
        {
            "schema_version": 1,
            "run_id": "run-1",
            "mode": "forge_swarm",
            "kind": "lock",
            "pid": 9_999_999,
            "hostname": "different-host",
            "acquired_at": "2026-03-26T00:00:00+00:00",
            "owner_token": "ambiguous-owner",
            "workspace_root": os.fspath(tmp_path),
            "run_dir": os.fspath(run_dir),
        },
    )

    with pytest.raises(
        RunMutationConflictError,
        match="Another Forge execution is already mutating this run",
    ):
        acquire_run_mutation_guard(
            run_id="run-1",
            mode="forge_swarm",
            run_dir=run_dir,
            workspace_root=tmp_path,
        )

    assert inspect_run_mutation_lock(run_dir) is not None


def test_windows_stale_lock_probe_does_not_send_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    metadata = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
    }

    def fail_kill(_pid: int, _signal: int) -> None:
        raise AssertionError("Windows stale-lock probes must not call os.kill")

    monkeypatch.setattr(run_lock_mod.os, "name", "nt", raising=False)
    monkeypatch.setattr(run_lock_mod.os, "kill", fail_kill)
    monkeypatch.setattr(run_lock_mod, "_windows_process_is_running", lambda _pid: True)

    assert run_lock_mod._definitely_stale_reason(metadata) is None


def test_windows_stale_lock_probe_recovers_missing_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = {
        "pid": 9_999_999,
        "hostname": socket.gethostname(),
    }

    monkeypatch.setattr(run_lock_mod.os, "name", "nt", raising=False)
    monkeypatch.setattr(run_lock_mod, "_windows_process_is_running", lambda _pid: False)

    assert run_lock_mod._definitely_stale_reason(metadata) == "owner process is no longer running"


def test_run_mutation_guard_recovery_handoff_returns_conflict(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    lock_path = run_dir / "active_execution.lock.json"
    write_run_mutation_lock_metadata(
        lock_path,
        {
            "schema_version": 1,
            "run_id": "run-1",
            "mode": "forge_swarm",
            "kind": "lock",
            "pid": 9_999_999,
            "hostname": socket.gethostname(),
            "acquired_at": "2026-03-26T00:00:00+00:00",
            "owner_token": "stale-owner",
            "workspace_root": os.fspath(tmp_path),
            "run_dir": os.fspath(run_dir),
        },
    )

    original_write_exclusive = run_lock_mod._write_exclusive
    call_count = 0

    def fake_write_exclusive(path: Path, text: str) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 3:
            payload = json.loads(text)
            payload["owner_token"] = "other-owner"
            write_run_mutation_lock_metadata(path, payload)
            raise FileExistsError(path)
        original_write_exclusive(path, text)

    monkeypatch.setattr(run_lock_mod, "_write_exclusive", fake_write_exclusive)

    with pytest.raises(
        RunMutationConflictError,
        match="another execution claimed the run while stale-lock recovery was finalizing",
    ):
        acquire_run_mutation_guard(
            run_id="run-1",
            mode="forge_swarm",
            run_dir=run_dir,
            workspace_root=tmp_path,
        )

    metadata = inspect_run_mutation_lock(run_dir)
    assert metadata is not None
    assert metadata["owner_token"] == "other-owner"
