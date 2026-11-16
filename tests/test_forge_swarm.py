from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console
from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code import workspace_binding as workspace_binding_mod
from alysis_code.cli import app as alysis_app
from alysis_code.config import AppConfig, clone_cfg
from alysis_code.conflict_auto_resolver import (
    AutoResolveOutcome,
    ConflictAutoResolveSettings,
)
from alysis_code.failure_category import FailureCategory
from alysis_code.forge import (
    add_task,
    create_plan_run,
    load_plan,
    save_plan,
)
from alysis_code.git_ops import GitOpsError
from alysis_code.integration_gate import IntegrationGateResult
from alysis_code.knowledge_base import load_knowledge_index, write_task_attempt_entry
from alysis_code.knowledge_capture import persist_execution_knowledge_capture
from alysis_code.llm.openai_compat import LLMError
from alysis_code.merge_conflict_reviewer import ConflictReviewOutcome
from alysis_code.plan_assistant import PlannerTurnResult
from alysis_code.plan_validation import PlannerFailedError
from alysis_code.remote_sync import RemoteSettings
from alysis_code.replanning import ReplanAttemptResult, run_replanning_attempt
from alysis_code.review_gate import ReviewOutcome
from alysis_code.run_lock import write_run_mutation_lock_metadata
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.swarm_backend import PreparedTaskWorkspace
from alysis_code.swarm_orchestrator import (
    _worker_result_nonexecuting_verification_reason,
    run_swarm,
)
from alysis_code.swarm_trace import SwarmWorkerTraceSurface, build_swarm_trace_event
from alysis_code.swarm_worker import TaskWorkerResult, run_task_worker
from alysis_code.verify_gate import VerifyCommandResult, VerifyRunResult


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "cfg"),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
    }


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _null_console() -> Console:
    return Console(file=io.StringIO())


def _host_verify_cfg(cfg: AppConfig | None = None) -> AppConfig:
    effective = clone_cfg(cfg or AppConfig(model="test-model"))
    extra_fields = dict(effective.extra_fields)
    verify_sandbox = dict(extra_fields.get("verify_sandbox") or {})
    verify_sandbox.setdefault("mode", "off")
    extra_fields["verify_sandbox"] = verify_sandbox
    effective.extra_fields = extra_fields
    return effective


def _structured_capture_text() -> str:
    return "\n".join(
        [
            "Worker summary.",
            "",
            "```knowledge_capture_json",
            json.dumps(
                {
                    "schema_version": 1,
                    "facts": [
                        {
                            "title": "Worker observed parser retry logic",
                            "summary": "Accepted worker output touched src/example.py retry behavior.",
                            "paths": ["src/example.py"],
                            "tags": ["parser", "retry"],
                        }
                    ],
                    "decisions": [
                        {
                            "decision_key": "worker-parser-retry",
                            "title": "Keep worker parser retry behavior",
                            "summary": "Use the accepted worker parser retry behavior.",
                            "status": "active",
                            "paths": ["src/example.py"],
                            "tags": ["parser", "retry"],
                        }
                    ],
                },
                indent=2,
                sort_keys=True,
            ),
            "```",
        ]
    )


def _init_git_repo_with_head(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-b", "main"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "init",
        ],
        cwd=repo,
        check=True,
    )


def _ok_worker_result(
    *,
    run_dir: Path,
    task_id: str,
    branch: str,
    knowledge_capture_artifact_dir: str | None = None,
    task_attempt_entry_id: str | None = None,
    task_attempt_knowledge_file_path: str | None = None,
    commit_hash: str | None = None,
    changed_files: list[str] | None = None,
    result_kind: str | None = None,
    noop_reason: str | None = None,
    agent_exit_code: int = 0,
    salvaged_nonzero_exit: bool = False,
    salvaged_agent_exception: bool = False,
    agent_exception_summary: str | None = None,
    verify_summary: str = "verification passed (2/2)",
    verify_payload: dict[str, object] | None = None,
    verify_command_source: str | None = None,
) -> TaskWorkerResult:
    return TaskWorkerResult(
        task_id=task_id,
        title=f"title-{task_id}",
        branch=branch,
        worktree_path=os.fspath(run_dir / "worktrees" / task_id / "repo"),
        started_at="2026-02-19T00:00:00+00:00",
        finished_at="2026-02-19T00:01:00+00:00",
        success=True,
        summary="ok",
        commit_hash=(
            commit_hash
            if commit_hash is not None or result_kind == "success_noop"
            else f"commit-{task_id}"
        ),
        error=None,
        report_path=f".alysis/runs/x/execution/reports/{task_id}.md",
        patch_path=f".alysis/runs/x/execution/patches/{task_id}.diff",
        log_path=f".alysis/runs/x/execution/logs/{task_id}.jsonl",
        log_pointer_path=f".alysis/runs/x/execution/logs/{task_id}.log.json",
        warnings=[],
        changed_files=(
            list(changed_files)
            if changed_files is not None
            else ([] if result_kind == "success_noop" else ["src/example.py"])
        ),
        verify_failed=False,
        verify_summary=verify_summary,
        verify_artifact_path=f".alysis/runs/x/execution/verify/{task_id}.txt",
        knowledge_capture_artifact_dir=knowledge_capture_artifact_dir,
        task_attempt_entry_id=task_attempt_entry_id,
        task_attempt_knowledge_file_path=task_attempt_knowledge_file_path,
        verify_payload=verify_payload,
        verify_command_source=verify_command_source,
        agent_exit_code=agent_exit_code,
        salvaged_nonzero_exit=salvaged_nonzero_exit,
        salvaged_agent_exception=salvaged_agent_exception,
        agent_exception_summary=agent_exception_summary,
        result_kind=result_kind,
        noop_reason=noop_reason,
    )


def test_swarm_nonexecuting_verification_reason_uses_shared_benign_predicate(
    tmp_path: Path,
) -> None:
    result = _ok_worker_result(
        run_dir=tmp_path,
        task_id="T01",
        branch="feat/t01",
        verify_summary="verification skipped: nothing to verify (1/1)",
        verify_payload={
            "commands": ["go test ./..."],
            "all_passed": True,
            "command_results": [
                {
                    "command": "go test ./...",
                    "effective_command": "go test ./...",
                    "exit_code": 0,
                    "status": "skipped",
                    "ok": True,
                    "real_execution": False,
                    "non_execution_reason": "go_test_no_test_files",
                }
            ],
        },
    )

    assert _worker_result_nonexecuting_verification_reason(result) is None


def _persist_worker_capture_artifact(
    *,
    paths,
    task: dict[str, object],
    source: str = "swarm_worker",
) -> Path:
    artifact_dir = paths.execution_knowledge_capture_dir / str(task["id"]) / "attempt_001"
    persist_execution_knowledge_capture(
        paths=paths,
        task=task,
        source=source,
        assistant_message=_structured_capture_text(),
        artifact_dir=artifact_dir,
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
    )
    return artifact_dir


def _persist_worker_task_attempt(
    *,
    paths,
    task: dict[str, object],
    acceptance_state: str = "pending",
):
    return write_task_attempt_entry(
        paths=paths,
        task=task,
        source="swarm_worker",
        result="success",
        summary="Worker completed successfully.",
        changed_files=["src/example.py"],
        verify_summary="verification passed (2/2)",
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
        acceptance_state=acceptance_state,
        extra_tags=["execution", "worker"],
    )


def _snapshot_worker_result(
    *,
    run_paths,
    task: dict[str, object],
    changed_files: list[str],
) -> TaskWorkerResult:
    task_id = str(task["id"])
    branch = str(task["branch"])
    repo_path = run_paths.run_dir / "worktrees" / task_id / "repo"
    commit_hash = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return TaskWorkerResult(
        task_id=task_id,
        title=str(task["title"]),
        branch=branch,
        worktree_path=os.fspath(repo_path),
        started_at="2026-02-19T00:00:00+00:00",
        finished_at="2026-02-19T00:01:00+00:00",
        success=True,
        summary="ok",
        commit_hash=commit_hash,
        error=None,
        report_path=f".alysis/runs/x/execution/reports/{task_id}.md",
        patch_path=f".alysis/runs/x/execution/patches/{task_id}.diff",
        log_path=f".alysis/runs/x/execution/logs/{task_id}.jsonl",
        log_pointer_path=f".alysis/runs/x/execution/logs/{task_id}.log.json",
        warnings=[],
        changed_files=changed_files,
        verify_failed=False,
        verify_summary="verification passed (1/1)",
        verify_artifact_path=f".alysis/runs/x/execution/verify/{task_id}.txt",
    )


def _worker_result_for_path(
    *,
    run_paths,
    task_id: str,
    branch: str,
    worktree_path: Path,
    success: bool,
    summary: str,
    commit_hash: str | None,
    error: str | None = None,
    changed_files: list[str] | None = None,
    verify_failed: bool = False,
    verify_summary: str = "verification passed (1/1)",
) -> TaskWorkerResult:
    return TaskWorkerResult(
        task_id=task_id,
        title=f"title-{task_id}",
        branch=branch,
        worktree_path=os.fspath(worktree_path),
        started_at="2026-02-19T00:00:00+00:00",
        finished_at="2026-02-19T00:01:00+00:00",
        success=success,
        summary=summary,
        commit_hash=commit_hash,
        error=error,
        report_path=f".alysis/runs/x/execution/reports/{task_id}.md",
        patch_path=f".alysis/runs/x/execution/patches/{task_id}.diff",
        log_path=f".alysis/runs/x/execution/logs/{task_id}.jsonl",
        log_pointer_path=f".alysis/runs/x/execution/logs/{task_id}.log.json",
        warnings=[],
        changed_files=changed_files or ["src/example.py"],
        verify_failed=verify_failed,
        verify_summary=verify_summary,
        verify_artifact_path=f".alysis/runs/x/execution/verify/{task_id}.txt",
    )


def _commit_repo_update(worktree_repo_path: Path, relative_path: str, content: str) -> str:
    target = worktree_repo_path / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", worktree_repo_path, "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            worktree_repo_path,
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "task update",
        ],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", worktree_repo_path, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _thread_diagnostics(
    *,
    thread: threading.Thread,
    result: dict[str, object],
) -> str:
    active_threads = ", ".join(
        f"{item.name}(daemon={item.daemon}, alive={item.is_alive()})"
        for item in threading.enumerate()
    )
    return (
        f"{thread.name} did not finish cleanly; "
        f"alive={thread.is_alive()}; result={result!r}; active_threads=[{active_threads}]"
    )


def test_run_swarm_queues_concurrent_same_workspace_writer_and_revalidates_plan(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-b", "main"], cwd=repo, check=True)
    (repo / "src.txt").write_text("before\n", encoding="utf-8")

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src.txt"], branch="feat/t01-a")
    save_plan(paths, plan)

    worker_started = threading.Event()
    release_worker = threading.Event()
    first_finished = threading.Event()
    second_finished = threading.Event()
    second_worker_started = threading.Event()
    first_result: dict[str, object] = {}
    second_result: dict[str, object] = {}

    def blocking_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        worktree_repo_path = Path(kwargs["worktree_repo_path"])
        worker_started.set()
        assert release_worker.wait(timeout=10)
        (worktree_repo_path / "src.txt").write_text("after\n", encoding="utf-8")
        subprocess.run(["git", "-C", worktree_repo_path, "add", "-A"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                worktree_repo_path,
                "-c",
                "user.name=Test User",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "task update",
            ],
            check=True,
        )
        return _snapshot_worker_result(run_paths=paths, task=task, changed_files=["src.txt"])

    def run_first_swarm() -> None:
        try:
            first_result["code"] = run_swarm(
                paths=paths,
                plan=load_plan(paths),
                cfg=AppConfig(model="test-model"),
                mode="auto",
                yes=False,
                max_steps=10,
                api_key_override="k",
                no_log=False,
                parallel=1,
                base_branch="main",
                max_tasks=None,
                max_attempts=None,
                dry_run=False,
                keep_worktrees=True,
                retry_failed=False,
                retry_changes_requested=False,
                only=None,
                retry_merge_conflicts=False,
                review=False,
                verify_mode="off",
                console=_null_console(),
                worker_runner=blocking_worker_runner,
                current_branch_fn=lambda _root: "main",
            )
        except BaseException as exc:  # noqa: BLE001
            first_result["exc"] = exc
        finally:
            first_finished.set()

    first_thread = threading.Thread(
        target=run_first_swarm,
        name="forge-concurrent-run-test",
        daemon=True,
    )
    first_thread.start()
    try:
        assert worker_started.wait(timeout=10), _thread_diagnostics(
            thread=first_thread,
            result=first_result,
        )

        def run_second_swarm() -> None:
            try:
                second_result["code"] = run_swarm(
                    paths=paths,
                    plan=load_plan(paths),
                    cfg=AppConfig(model="test-model"),
                    mode="auto",
                    yes=False,
                    max_steps=10,
                    api_key_override="k",
                    no_log=False,
                    parallel=1,
                    base_branch="main",
                    max_tasks=None,
                    max_attempts=None,
                    dry_run=False,
                    keep_worktrees=True,
                    retry_failed=False,
                    retry_changes_requested=False,
                    only=None,
                    retry_merge_conflicts=False,
                    review=False,
                    verify_mode="off",
                    console=_null_console(),
                    worker_runner=lambda **_kwargs: (
                        second_worker_started.set(),
                        (_ for _ in ()).throw(
                            AssertionError("second swarm execution should not start workers")
                        ),
                    )[1],
                    current_branch_fn=lambda _root: "main",
                )
            except BaseException as exc:  # noqa: BLE001
                second_result["exc"] = exc
            finally:
                second_finished.set()

        second_thread = threading.Thread(
            target=run_second_swarm,
            name="forge-concurrent-run-second-test",
            daemon=True,
        )
        second_thread.start()
        wait_dir = paths.runtime_dir / "workspace_execution"
        for _ in range(40):
            if list(wait_dir.glob("active_execution.waiting.*.json")):
                break
            if second_finished.is_set():
                break
            threading.Event().wait(0.05)
        assert list(wait_dir.glob("active_execution.waiting.*.json"))
        assert not second_finished.is_set()
        assert not second_worker_started.is_set()
    finally:
        release_worker.set()
        first_thread.join(timeout=10)

    assert first_finished.is_set(), _thread_diagnostics(
        thread=first_thread,
        result=first_result,
    )
    assert not first_thread.is_alive(), _thread_diagnostics(
        thread=first_thread,
        result=first_result,
    )

    assert "exc" not in first_result
    assert first_result["code"] == 0
    assert (repo / "src.txt").read_text(encoding="utf-8") == "after\n"
    assert second_finished.wait(timeout=10), _thread_diagnostics(
        thread=second_thread,
        result=second_result,
    )
    # The event is set in the thread's ``finally``; the thread still has to unwind
    # before it stops, so wait for it rather than sampling it at that instant.
    second_thread.join(timeout=10)
    assert not second_thread.is_alive(), _thread_diagnostics(
        thread=second_thread,
        result=second_result,
    )
    assert "exc" not in second_result
    assert second_result["code"] == 0
    assert not second_worker_started.is_set()

    worker_results = list(paths.execution_dir.glob("worker_results/*.json"))
    merge_results = list(paths.execution_dir.glob("merge_results/*.json"))
    assert [path.name for path in worker_results] == ["T01.json"]
    assert [path.name for path in merge_results] == ["T01.json"]
    revalidation = json.loads(
        (paths.execution_dir / "concurrency_revalidation.json").read_text(encoding="utf-8")
    )
    assert revalidation["reason_code"] == "queued_execution_revalidated"
    assert revalidation["plan_reloaded"] is True
    summary = (paths.execution_dir / "swarm_summary.md").read_text(encoding="utf-8")
    assert "Another Forge execution is already mutating" not in summary


def test_forge_swarm_cli_reports_structured_timeout_when_same_run_stays_locked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(plan, title="Task A", estimated_files=["src.txt"], branch="feat/t01-a")
    save_plan(paths, plan)
    plan_snapshot = paths.plan_json_path.read_text(encoding="utf-8")

    write_run_mutation_lock_metadata(
        paths.run_dir / "active_execution.lock.json",
        {
            "schema_version": 1,
            "run_id": paths.run_id,
            "mode": "forge_swarm:other",
            "kind": "lock",
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "acquired_at": "2026-03-26T00:00:00+00:00",
            "owner_token": "other-owner",
            "workspace_root": os.fspath(paths.root),
            "run_dir": os.fspath(paths.run_dir),
        },
    )

    monkeypatch.setattr(
        cli_mod,
        "run_swarm",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("run_swarm should not be called when the run is already locked")
        ),
    )
    monkeypatch.setattr(cli_mod, "resolve_model_for_role", lambda **_kwargs: "test-model")

    result = runner.invoke(
        alysis_app,
        ["forge", "swarm", "--path", os.fspath(repo), "--mode", "auto"],
        env={**_env(tmp_path), "ALYSIS_FORGE_LOCK_WAIT_TIMEOUT_S": "0"},
    )

    assert result.exit_code == 2
    assert "already mutating this run" in result.output
    assert paths.plan_json_path.read_text(encoding="utf-8") == plan_snapshot
    assert list(paths.execution_dir.glob("worker_results/*.json")) == []
    assert list(paths.execution_dir.glob("merge_results/*.json")) == []
    assert not (paths.execution_dir / "swarm_summary.md").exists()


def test_forge_swarm_reports_timeout_when_same_workspace_stays_locked_by_another_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    first_paths = create_plan_run(repo)
    second_paths = create_plan_run(repo)
    plan = load_plan(second_paths)
    add_task(plan, title="Task B", estimated_files=["src.txt"], branch="feat/t01-b")
    save_plan(second_paths, plan)
    plan_snapshot = second_paths.plan_json_path.read_text(encoding="utf-8")
    workspace_lock_dir = second_paths.runtime_dir / "workspace_execution"
    workspace_lock_dir.mkdir(parents=True, exist_ok=True)

    write_run_mutation_lock_metadata(
        workspace_lock_dir / "active_execution.lock.json",
        {
            "schema_version": 1,
            "run_id": f"workspace:{os.fspath(repo.resolve())}",
            "mode": "forge_swarm:other:workspace",
            "kind": "lock",
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "acquired_at": "2026-03-26T00:00:00+00:00",
            "owner_token": "other-owner",
            "workspace_root": os.fspath(repo.resolve()),
            "run_dir": os.fspath(first_paths.run_dir),
        },
    )

    monkeypatch.setenv("ALYSIS_FORGE_LOCK_WAIT_TIMEOUT_S", "0")
    with pytest.raises(
        Exception, match="Another Forge execution is already mutating this workspace"
    ):
        run_swarm(
            paths=second_paths,
            plan=load_plan(second_paths),
            cfg=AppConfig(model="test-model"),
            mode="auto",
            yes=False,
            max_steps=10,
            api_key_override="k",
            no_log=False,
            parallel=1,
            base_branch=None,
            max_tasks=None,
            max_attempts=None,
            dry_run=False,
            keep_worktrees=True,
            retry_failed=False,
            retry_changes_requested=False,
            only=None,
            retry_merge_conflicts=False,
            review=False,
            verify_mode="off",
            console=_null_console(),
            worker_runner=lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("workspace-locked swarm should not start workers")
            ),
        )

    assert second_paths.plan_json_path.read_text(encoding="utf-8") == plan_snapshot
    assert list(second_paths.execution_dir.glob("worker_results/*.json")) == []
    assert list(second_paths.execution_dir.glob("merge_results/*.json")) == []
    assert not (second_paths.execution_dir / "swarm_summary.md").exists()


def _integration_result(
    *,
    paths,
    batch_index: int,
    mode: str,
    merged_task_ids: list[str],
    passed: bool,
    summary: str,
    phase: str = "post_merge",
) -> IntegrationGateResult:
    batch_label = f"batch_{batch_index:03d}"
    artifact_dir = paths.execution_integration_dir / batch_label
    artifact_dir.mkdir(parents=True, exist_ok=True)
    verify_path = artifact_dir / "verify.txt"
    commands_path = artifact_dir / "commands.json"
    stdout_path = artifact_dir / "stdout.txt"
    stderr_path = artifact_dir / "stderr.txt"
    summary_path = artifact_dir / "summary.md"
    result_path = artifact_dir / "result.json"
    for path in (verify_path, commands_path, stdout_path, stderr_path, summary_path, result_path):
        path.write_text(f"{path.name}\n", encoding="utf-8")
    verify_result = VerifyRunResult(
        commands=["pytest -q"],
        command_results=[
            VerifyCommandResult(
                command="pytest -q",
                exit_code=0 if passed else 1,
                output=summary,
                stdout="" if not passed else "ok\n",
                stderr="" if passed else "failed\n",
                real_execution=True,
            )
        ],
        artifact_path=verify_path,
    )
    return IntegrationGateResult(
        batch_index=batch_index,
        batch_label=batch_label,
        mode=mode,  # type: ignore[arg-type]
        command_source="test",
        commands=("pytest -q",),
        merged_task_ids=tuple(merged_task_ids),
        merged_paths=("src/example.py",),
        verify_result=verify_result,
        artifact_dir=artifact_dir,
        result_path=result_path,
        commands_path=commands_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        summary_path=summary_path,
        verify_artifact_path=verify_path,
        phase=phase,
        verified_root=paths.root,
    )


def _replan_result(
    *,
    paths,
    index: int,
    requested_mode: str = "suggest",
    effective_mode: str = "suggest",
    proposal_generated: bool = True,
    validation_passed: bool = True,
    applied: bool = False,
    plan_changed: bool = False,
    schedule_recomputed: bool = False,
) -> ReplanAttemptResult:
    label = f"replan_{index:03d}"
    artifact_dir = paths.plan_replans_dir / label
    artifact_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "selected_knowledge_manifest_path": artifact_dir / "selected_knowledge_manifest.json",
        "selected_knowledge_summary_path": artifact_dir / "selected_knowledge_summary.md",
        "evidence_path": artifact_dir / "evidence.json",
        "evidence_summary_path": artifact_dir / "evidence.md",
        "planner_result_path": artifact_dir / "planner_result.json",
        "plan_update_path": artifact_dir / "plan_update.json",
        "validation_path": artifact_dir / "validation.json",
        "summary_path": artifact_dir / "summary.md",
    }
    for path in files.values():
        path.write_text("{}\n", encoding="utf-8")
    return ReplanAttemptResult(
        replan_index=index,
        replan_label=label,
        requested_mode=requested_mode,  # type: ignore[arg-type]
        effective_mode=effective_mode,  # type: ignore[arg-type]
        trigger_reason="open integration issues remain",
        artifact_dir=artifact_dir,
        selected_knowledge_manifest_path=files["selected_knowledge_manifest_path"],
        selected_knowledge_summary_path=files["selected_knowledge_summary_path"],
        evidence_path=files["evidence_path"],
        evidence_summary_path=files["evidence_summary_path"],
        planner_result_path=files["planner_result_path"],
        plan_update_path=files["plan_update_path"],
        validation_path=files["validation_path"],
        summary_path=files["summary_path"],
        proposal_generated=proposal_generated,
        validation_passed=validation_passed,
        applied=applied,
        plan_changed=plan_changed,
        schedule_recomputed=schedule_recomputed,
        planner_error=None,
        plan_update_summary="updated tasks: T02" if proposal_generated else None,
    )


def test_swarm_dry_run_prints_schedule_and_does_not_execute(tmp_path: Path) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    t1 = add_task(
        plan,
        title="Task A",
        estimated_files=["src/a.py"],
        branch="feat/t01-a",
    )
    t2 = add_task(
        plan,
        title="Task B",
        estimated_files=["src/b.py"],
        branch="feat/t02-b",
    )
    save_plan(paths, plan)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "swarm",
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--base-branch",
            "main",
            "--parallel",
            "2",
            "--dry-run",
        ],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert "forge swarm (dry-run)" in result.output
    assert "Batch 1" in result.output
    worker_results = list(paths.execution_dir.glob("worker_results/*.json"))
    assert worker_results == []
    summary = (paths.execution_dir / "swarm_summary.md").read_text(encoding="utf-8")
    assert f"- Batch 1: {t1['id']}, {t2['id']}" in summary


def test_execution_ignores_superseded_tasks(tmp_path: Path) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    superseded = add_task(
        plan,
        title="Implement obsolete TOML settings",
        description="Update settings.toml behavior.",
        _allow_execution_unready=True,
    )
    superseded["estimated_files"] = []
    superseded["write_scope"] = []
    superseded["status"] = "superseded"
    active = add_task(
        plan,
        title="Implement APP_TIMEOUT_SECONDS env var timeout",
        description="Read APP_TIMEOUT_SECONDS in src/config.py.",
        estimated_files=["src/config.py"],
        write_scope=["src/config.py"],
        branch="feat/env-timeout",
    )
    save_plan(paths, plan)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "swarm",
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--base-branch",
            "main",
            "--dry-run",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert f"Runnable tasks: {active['id']}" in result.output
    assert str(superseded["id"]) not in result.output


def test_forge_swarm_cli_rejects_guarded_workspace_without_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(workspace_binding_mod, "_home_directory", lambda: home.resolve())

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "swarm",
            "--path",
            os.fspath(home),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--base-branch",
            "main",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 2
    assert "--allow-broad-workspace" in result.output


def test_forge_swarm_cli_rejects_blocked_workspace(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "swarm",
            "--path",
            os.fspath(Path(os.path.abspath(os.sep))),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--base-branch",
            "main",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 2
    assert "filesystem root '/'" in result.output


def test_forge_swarm_cli_missing_current_run_gives_actionable_error(tmp_path: Path) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "swarm",
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--base-branch",
            "main",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 2
    assert "No current forge run was found for this workspace." in result.output
    assert "alysis forge plan --path" in result.output


def test_swarm_summary_and_trace_include_binding_metadata(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)
    subdir = repo / "pkg"
    subdir.mkdir()
    binding = workspace_binding_mod.resolve_workspace_binding(subdir, source="explicit_path")

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=True,
        keep_worktrees=False,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        console=_null_console(),
        workspace_binding=binding,
    )

    assert code == 0
    summary = (paths.execution_dir / "swarm_summary.md").read_text(encoding="utf-8")
    assert f"Requested Path: `{subdir.resolve()}`" in summary
    assert f"Focus Directory: `{subdir.resolve()}`" in summary
    assert "Binding Risk Level: `healthy`" in summary
    assert "Broad Workspace Override Used: `no`" in summary
    assert f"- Batch 1: {task['id']}" in summary

    trace_path = paths.execution_dir / "trace" / "swarm_trace.jsonl"
    trace_events = [
        json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(str(event["phase"]) == "workspace.binding" for event in trace_events)
    assert any(
        str(event["phase"]) == "workspace.binding"
        and f"requested={subdir.resolve()}" in str(event["message"])
        for event in trace_events
    )


def test_run_swarm_metadata_warnings_emit_worker_warning_not_worker_error(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(
        plan,
        title="Metadata warning worker",
        estimated_files=["src/a.py"],
        branch="feat/t01-metadata-warning",
    )
    save_plan(paths, plan)

    def warning_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        trace_sink = kwargs["trace_sink"]
        trace_level = kwargs["trace_level"]
        task_payload = kwargs["task"]
        task_id = str(task_payload.get("id") or "T01")
        surface = SwarmWorkerTraceSurface(
            run_id=paths.run_id,
            task_id=task_id,
            trace_sink=trace_sink,
            trace_level=trace_level,
        )
        surface.on_warning(
            "Model metadata warning for unknown-model-xyz (roles: coding): "
            "fallback capacity metadata in context_window_tokens, max_output_tokens."
        )
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=task_id,
            branch=str(task_payload.get("branch") or "feat/t01-metadata-warning"),
            result_kind="success_noop",
            noop_reason="already_satisfied",
            commit_hash=None,
            changed_files=[],
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=True,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=False,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        console=_null_console(),
        scope_mode="warn",
        verify_mode="warn",
        trace_level="compact",
        worker_runner=warning_worker_runner,
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda *_a, **_k: None,
        branch_exists_fn=lambda *_a, **_k: True,
        current_branch_fn=lambda _root: "main",
    )

    assert code == 0
    trace_path = paths.execution_dir / "trace" / "swarm_trace.jsonl"
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    metadata_events = [
        event
        for event in events
        if "Model metadata warning for unknown-model-xyz" in str(event.get("message") or "")
    ]
    assert metadata_events
    assert any(str(event["phase"]) == "worker.warning" for event in metadata_events)
    assert not any(str(event["phase"]) == "worker.error" for event in metadata_events)


def test_forge_swarm_cli_passes_no_worker_max_steps_without_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(
        plan,
        title="Task A",
        estimated_files=["src/a.py"],
        branch="feat/t01-a",
    )
    save_plan(paths, plan)
    captured: dict[str, object] = {}

    def fake_run_swarm(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli_mod, "run_swarm", fake_run_swarm)
    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "swarm",
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--base-branch",
            "main",
        ],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert captured["max_steps"] is None


def test_forge_swarm_cli_defaults_scope_to_strict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)
    captured: dict[str, object] = {}

    def fake_run_swarm(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli_mod, "run_swarm", fake_run_swarm)
    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "swarm",
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert captured["scope_mode"] == "strict"


def test_forge_swarm_refuses_execution_unready_mutating_task_after_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    plan["tasks"].append(
        {
            "id": "T01",
            "title": "Fix login bug",
            "description": "Update auth flow.",
            "acceptance_criteria": ["Login works."],
            "dependencies": [],
            "estimated_files": [],
            "write_scope": [],
            "branch": "",
            "status": "planned",
            "attempts": 0,
        }
    )
    save_plan(paths, plan)

    def fake_run_swarm(**_kwargs: object) -> int:
        raise AssertionError("run_swarm should not be called for execution-unready tasks")

    monkeypatch.setattr(cli_mod, "run_swarm", fake_run_swarm)
    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "swarm",
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 2
    assert "Execution blocked" in result.output
    assert "runnable or ambiguous task lacks runnable estimated_files/write_scope" in result.output


def test_forge_swarm_refuses_ready_for_merge_task_without_runnable_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    plan["tasks"].append(
        {
            "id": "T01",
            "title": "Fix login bug",
            "description": "Update auth flow.",
            "acceptance_criteria": ["Login works."],
            "dependencies": [],
            "estimated_files": [],
            "write_scope": [],
            "branch": "feat/t01",
            "status": "ready_for_merge",
            "attempts": 1,
        }
    )
    save_plan(paths, plan)

    def fake_run_swarm(**_kwargs: object) -> int:
        raise AssertionError("run_swarm should not be called for execution-unready merge tasks")

    monkeypatch.setattr(cli_mod, "run_swarm", fake_run_swarm)
    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "swarm",
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 2
    assert "Execution blocked" in result.output
    assert "runnable or ambiguous task lacks runnable estimated_files/write_scope" in result.output


def test_forge_swarm_refuses_retry_merge_conflict_task_without_runnable_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    plan["tasks"].append(
        {
            "id": "T01",
            "title": "Fix login bug",
            "description": "Update auth flow.",
            "acceptance_criteria": ["Login works."],
            "dependencies": [],
            "estimated_files": [],
            "write_scope": [],
            "branch": "feat/t01",
            "status": "merge_conflict",
            "attempts": 1,
        }
    )
    save_plan(paths, plan)

    def fake_run_swarm(**_kwargs: object) -> int:
        raise AssertionError("run_swarm should not be called for execution-unready retry tasks")

    monkeypatch.setattr(cli_mod, "run_swarm", fake_run_swarm)
    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "swarm",
            "--path",
            os.fspath(repo),
            "--retry-merge-conflicts",
            "--model",
            "test-model",
            "--api-key",
            "k",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 2
    assert "Execution blocked" in result.output
    assert "runnable or ambiguous task lacks runnable estimated_files/write_scope" in result.output


def test_forge_swarm_cli_scope_warn_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)
    captured: dict[str, object] = {}

    def fake_run_swarm(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli_mod, "run_swarm", fake_run_swarm)
    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "swarm",
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--scope",
            "warn",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert captured["scope_mode"] == "warn"


def test_forge_swarm_cli_scope_off_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)
    captured: dict[str, object] = {}

    def fake_run_swarm(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli_mod, "run_swarm", fake_run_swarm)
    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "swarm",
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--scope",
            "off",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert captured["scope_mode"] == "off"


def test_run_swarm_defaults_scope_to_strict_internally(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_head(repo)
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)
    captured: dict[str, object] = {}

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        captured["scope_mode"] = kwargs["scope_mode"]
        t = kwargs["task"]
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(t["id"]),
            branch=str(t["branch"]),
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode="off",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        merge_runner=lambda *_a, **_k: "merge-commit",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )

    assert code == 0
    assert task["id"]
    assert captured["scope_mode"] == "strict"


def test_forge_swarm_cli_honors_max_steps_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(
        plan,
        title="Task A",
        estimated_files=["src/a.py"],
        branch="feat/t01-a",
    )
    save_plan(paths, plan)
    captured: dict[str, object] = {}

    def fake_run_swarm(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli_mod, "run_swarm", fake_run_swarm)
    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "swarm",
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--base-branch",
            "main",
            "--max-steps",
            "12",
        ],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert captured["max_steps"] == 12


def test_swarm_dry_run_allows_empty_verify_commands_config(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    cfg = AppConfig(model="test-model")
    cfg.verify_commands = []

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=cfg,
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=True,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="warn",
        verify_cmd=None,
        console=_null_console(),
        worker_runner=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("worker should not run in dry-run")
        ),
        merge_runner=lambda *_a, **_k: "merge",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 0


def test_run_swarm_refreshes_workspace_context_artifacts_on_startup(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)
    assert paths.workspace_context_json_path is not None
    assert paths.workspace_summary_md_path is not None
    paths.workspace_context_json_path.unlink()
    paths.workspace_summary_md_path.unlink()

    cfg = AppConfig(model="test-model")
    cfg.verify_commands = []

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=cfg,
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=True,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="warn",
        verify_cmd=None,
        console=_null_console(),
        worker_runner=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("worker should not run in dry-run")
        ),
        merge_runner=lambda *_a, **_k: "merge",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 0
    assert paths.workspace_context_json_path.exists()
    assert paths.workspace_summary_md_path.exists()
    swarm_summary = (paths.execution_dir / "swarm_summary.md").read_text(encoding="utf-8")
    assert "- Backend: `snapshot_workspace`" in swarm_summary
    assert "## Workspace Context" in swarm_summary
    assert "Workspace root:" in swarm_summary


def test_swarm_passes_resolved_verify_commands_to_custom_workers_when_config_is_default(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text(
        '{"scripts":{"test":"vitest run"}}\n',
        encoding="utf-8",
    )
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src/a.ts"], branch="feat/t01-a")
    save_plan(paths, plan)

    captured: dict[str, object] = {}

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        captured["verify_commands"] = list(kwargs["verify_commands"])
        captured["cfg_verify_commands"] = list(kwargs["cfg"].verify_commands)
        selection = kwargs["verify_command_selection"]
        captured["verify_command_selection_source"] = (
            selection.source if selection is not None else None
        )
        captured["verify_command_selection_commands"] = (
            list(selection.commands) if selection is not None else None
        )
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(kwargs["task"]["id"]),
            branch=str(kwargs["task"]["branch"]),
            verify_payload={"commands": list(kwargs["verify_commands"])},
            verify_command_source=str(captured["verify_command_selection_source"] or ""),
        )

    def fake_merge_runner(_root, *, base_branch: str, task_branch: str, message: str) -> str:
        assert task_branch == "feat/t01-a"
        assert message.startswith("Merge ")
        return "merge-feat/t01-a"

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="strict",
        integration_mode="off",
        verify_cmd=None,
        console=_null_console(),
        worker_runner=fake_worker_runner,
        integration_runner=lambda **kwargs: _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=list(kwargs["merged_task_ids"]),
            passed=True,
            summary="integration passed",
            phase=str(kwargs.get("phase") or "post_merge"),
        ),
        merge_runner=fake_merge_runner,
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )

    assert code == 0
    assert task["id"]
    assert captured["verify_commands"] == ["npm test"]
    assert captured["cfg_verify_commands"] == ["npm test"]
    assert captured["verify_command_selection_source"] == "repo_scan.likely_test_commands"
    assert captured["verify_command_selection_commands"] == ["npm test"]

    worker_result = _load_json(paths.execution_dir / "worker_results" / f"{task['id']}.json")
    assert worker_result["verify_command_source"] == "repo_scan.likely_test_commands"
    assert worker_result["verify_payload"]["commands"] == ["npm test"]


def test_swarm_custom_worker_receives_refined_node_test_authoritative_contract(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Task A",
        estimated_files=["test/app.test.js", "src/app.js"],
        branch="feat/t01-a",
    )
    task["write_scope"] = ["test/app.test.js", "src/app.js"]
    save_plan(paths, plan)

    captured: dict[str, object] = {}

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        captured["verify_commands"] = list(kwargs["verify_commands"])
        captured["cfg_verify_commands"] = list(kwargs["cfg"].verify_commands)
        selection = kwargs["verify_command_selection"]
        captured["verify_command_selection_source"] = (
            selection.source if selection is not None else None
        )
        captured["verify_command_selection_commands"] = (
            list(selection.commands) if selection is not None else None
        )
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(kwargs["task"]["id"]),
            branch=str(kwargs["task"]["branch"]),
            result_kind="success_noop",
            verify_summary="verification passed (1/1)",
            verify_payload={"commands": list(kwargs["verify_commands"]), "all_passed": True},
            verify_command_source=str(captured["verify_command_selection_source"] or ""),
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model", verify_commands=["pytest -q", "ruff check ."]),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="warn",
        integration_mode="off",
        verify_cmd=None,
        console=_null_console(),
        worker_runner=fake_worker_runner,
        integration_runner=lambda **kwargs: _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=list(kwargs["merged_task_ids"]),
            passed=True,
            summary="integration passed",
            phase=str(kwargs.get("phase") or "post_merge"),
        ),
        merge_runner=lambda *_a, **_k: "merge-feat/t01-a",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )

    assert code == 0
    assert captured["verify_commands"] == ["node --test"]
    assert captured["cfg_verify_commands"] == ["node --test"]
    assert captured["verify_command_selection_source"] == "task_refinement.node_test"
    assert captured["verify_command_selection_commands"] == ["node --test"]

    worker_result = _load_json(paths.execution_dir / "worker_results" / f"{task['id']}.json")
    assert worker_result["result_kind"] == "success_noop"
    assert worker_result["verify_command_source"] == "task_refinement.node_test"
    assert worker_result["verify_payload"]["commands"] == ["node --test"]


def test_swarm_refines_generic_fallback_to_node_test_for_js_task_reports(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Task A",
        estimated_files=["test/app.test.js", "src/app.js"],
        branch="feat/t01-a",
    )
    task["write_scope"] = ["test/app.test.js", "src/app.js"]
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)
    captured: dict[str, object] = {}

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["authoritative_verification_commands"] = kwargs.get(
            "authoritative_verification_commands"
        )
        captured["runtime_kind"] = kwargs.get("runtime_kind")
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.build_execution_reporting_diff_with_commit_range",
        lambda *_a, **_k: SimpleNamespace(changed_files=(), patch_text=""),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: [],
    )

    def fake_verify(**kwargs):  # type: ignore[no-untyped-def]
        captured["verify_commands"] = list(kwargs["commands"])
        artifact_path = kwargs["artifact_path"]
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("ok\n", encoding="utf-8")
        return VerifyRunResult(
            commands=list(kwargs["commands"]),
            command_results=[
                VerifyCommandResult(
                    command=kwargs["commands"][0],
                    exit_code=0,
                    output="1 passed\n",
                    stdout="1 passed\n",
                    real_execution=True,
                )
            ],
            artifact_path=artifact_path,
        )

    monkeypatch.setattr("alysis_code.swarm_worker.run_task_verification", fake_verify)

    def ensure_worktree(**kwargs):  # type: ignore[no-untyped-def]
        task_obj = kwargs["task"]
        control_root = paths.run_dir / "worktrees" / str(task_obj["id"])
        control_root.mkdir(parents=True, exist_ok=True)
        return PreparedTaskWorkspace(
            backend_name="git_worktree",
            task_id=str(task_obj["id"]),
            branch=str(task_obj["branch"]),
            base_branch=str(kwargs["base_branch"]),
            worktree_path=worktree_repo,
            control_root=control_root,
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=True,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="warn",
        verify_cmd=None,
        console=_null_console(),
        worker_runner=run_task_worker,
        ensure_worktree_fn=ensure_worktree,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )

    assert code == 0
    assert captured["authoritative_verification_commands"] == ["node --test"]
    assert captured["runtime_kind"] == RuntimeKind.SWARM_WORKER
    assert captured["verify_commands"] == ["node --test"]

    worker_result = _load_json(paths.execution_dir / "worker_results" / f"{task['id']}.json")
    assert worker_result["result_kind"] == "success_noop"
    assert worker_result["verify_command_source"] == "task_refinement.node_test"
    assert worker_result["verify_payload"]["commands"] == ["node --test"]

    report = (paths.execution_reports_dir / f"{task['id']}.md").read_text(encoding="utf-8")
    assert "- Verify Command Source: task_refinement.node_test" in report
    assert "- `node --test`" in report


def test_swarm_strict_scope_bootstrap_exact_file_task_filters_ancestor_placeholders_and_egg_info(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Bootstrap calcbox",
        estimated_files=[
            "pyproject.toml",
            "src/calcbox/__init__.py",
            "src/calcbox/core.py",
        ],
        branch="feat/t01-a",
    )
    save_plan(paths, plan)

    def fake_run_agent(*, root: Path, **_kwargs) -> int:
        (root / "pyproject.toml").write_text("[project]\nname='calcbox'\n", encoding="utf-8")
        (root / "src" / "calcbox").mkdir(parents=True, exist_ok=True)
        (root / "src" / "calcbox" / "__init__.py").write_text("", encoding="utf-8")
        (root / "src" / "calcbox" / "core.py").write_text(
            "def add(a, b):\n    return a + b\n",
            encoding="utf-8",
        )
        egg_info = root / "src" / "calcbox.egg-info"
        egg_info.mkdir(parents=True, exist_ok=True)
        (egg_info / "PKG-INFO").write_text("Metadata-Version: 2.4\n", encoding="utf-8")
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=True,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        scope_mode="strict",
        verify_mode="off",
        integration_mode="off",
        console=_null_console(),
        worker_runner=run_task_worker,
        merge_runner=lambda *_a, **_k: "applied123",
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )

    assert code == 0
    worker_result = _load_json(paths.execution_dir / "worker_results" / f"{task['id']}.json")
    assert worker_result["success"] is True
    assert worker_result["changed_files"] == [
        "pyproject.toml",
        "src/calcbox/__init__.py",
        "src/calcbox/core.py",
    ]
    assert all(".egg-info/" not in path for path in worker_result["changed_files"])

    final_plan = _load_json(paths.plan_json_path)
    assert final_plan["tasks"][0]["status"] == "done"

    report = (paths.execution_reports_dir / f"{task['id']}.md").read_text(encoding="utf-8")
    assert "strict scope isolation" not in report
    assert ".egg-info" not in report

    summary = (paths.execution_dir / "swarm_summary.md").read_text(encoding="utf-8")
    assert ".egg-info" not in summary
    assert "strict scope isolation" not in summary
    trace_events = [
        json.loads(line)
        for line in (paths.execution_dir / "trace" / "swarm_trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert any(
        event.get("task_id") == task["id"]
        and event.get("phase") == "worker.lifecycle"
        and "Worker finished successfully." in str(event.get("message"))
        for event in trace_events
    )


def test_swarm_refines_generic_fallback_to_node_test_from_structured_task_text(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Task A",
        estimated_files=["src/app.js"],
        branch="feat/t01-a",
    )
    task["write_scope"] = ["src/app.js"]
    task["acceptance_criteria"] = ["Use node --test for test verification."]
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)
    captured: dict[str, object] = {}

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["authoritative_verification_commands"] = kwargs.get(
            "authoritative_verification_commands"
        )
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.build_execution_reporting_diff_with_commit_range",
        lambda *_a, **_k: SimpleNamespace(changed_files=(), patch_text=""),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: [],
    )

    def fake_verify(**kwargs):  # type: ignore[no-untyped-def]
        captured["verify_commands"] = list(kwargs["commands"])
        artifact_path = kwargs["artifact_path"]
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("ok\n", encoding="utf-8")
        return VerifyRunResult(
            commands=list(kwargs["commands"]),
            command_results=[
                VerifyCommandResult(
                    command=kwargs["commands"][0],
                    exit_code=0,
                    output="1 passed\n",
                    stdout="1 passed\n",
                    real_execution=True,
                )
            ],
            artifact_path=artifact_path,
        )

    monkeypatch.setattr("alysis_code.swarm_worker.run_task_verification", fake_verify)

    def ensure_worktree(**kwargs):  # type: ignore[no-untyped-def]
        task_obj = kwargs["task"]
        control_root = paths.run_dir / "worktrees" / str(task_obj["id"])
        control_root.mkdir(parents=True, exist_ok=True)
        return PreparedTaskWorkspace(
            backend_name="git_worktree",
            task_id=str(task_obj["id"]),
            branch=str(task_obj["branch"]),
            base_branch=str(kwargs["base_branch"]),
            worktree_path=worktree_repo,
            control_root=control_root,
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=True,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="warn",
        verify_cmd=None,
        console=_null_console(),
        worker_runner=run_task_worker,
        ensure_worktree_fn=ensure_worktree,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )

    assert code == 0
    assert captured["authoritative_verification_commands"] == ["node --test"]
    assert captured["verify_commands"] == ["node --test"]

    worker_result = _load_json(paths.execution_dir / "worker_results" / f"{task['id']}.json")
    assert worker_result["result_kind"] == "success_noop"
    assert worker_result["verify_command_source"] == "task_refinement.node_test"
    assert worker_result["verify_payload"]["commands"] == ["node --test"]

    report = (paths.execution_reports_dir / f"{task['id']}.md").read_text(encoding="utf-8")
    assert "- Verify Command Source: task_refinement.node_test" in report
    assert "- `node --test`" in report
    assert "pytest -q" not in report

    trace_events = [
        json.loads(line)
        for line in (paths.execution_dir / "trace" / "swarm_trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert any(
        event.get("phase") == "verify.lifecycle"
        and "task_refinement.node_test" in str(event.get("message"))
        and "node --test" in str(event.get("message"))
        for event in trace_events
    )


def test_run_swarm_writes_unified_trace_artifact_for_parallel_workers(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    add_task(plan, title="Task B", estimated_files=["src/b.py"], branch="feat/t02-b")
    save_plan(paths, plan)

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        task = kwargs["task"]
        trace_sink = kwargs["trace_sink"]
        run_paths = kwargs["run_paths"]
        task_id = str(task["id"])
        trace_sink.emit(
            build_swarm_trace_event(
                run_id=run_paths.run_id,
                task_id=task_id,
                phase="worker.progress",
                message="Synthetic worker progress.",
            )
        )
        return _ok_worker_result(
            run_dir=run_paths.run_dir,
            task_id=task_id,
            branch=str(task["branch"]),
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=True,
        parallel=2,
        base_branch=None,
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=False,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        console=_null_console(),
        scope_mode="warn",
        verify_mode="off",
        integration_mode="off",
        review=False,
        trace_level="compact",
        worker_runner=fake_worker_runner,
        merge_runner=lambda *_a, **_k: "merge-commit",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda *_a, **_k: None,
        branch_exists_fn=lambda *_a, **_k: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 0
    trace_path = paths.execution_dir / "trace" / "swarm_trace.jsonl"
    assert trace_path.exists()
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    phases = {str(event["phase"]) for event in events}
    messages = [str(event["message"]) for event in events]
    task_ids = {str(event.get("task_id") or "") for event in events}
    assert "swarm.backend" in phases
    assert "swarm.lifecycle" in phases
    assert "scheduler.batch" in phases
    assert "worker.lifecycle" in phases
    assert "worker.progress" in phases
    assert "merge.lifecycle" in phases
    assert any("Using backend snapshot_workspace." in message for message in messages)
    assert "T01" in task_ids
    assert "T02" in task_ids


def test_run_swarm_flushes_trace_artifact_on_worker_failure(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    def failing_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        run_paths = kwargs["run_paths"]
        trace_sink = kwargs["trace_sink"]
        trace_sink.emit(
            build_swarm_trace_event(
                run_id=run_paths.run_id,
                task_id="T01",
                phase="worker.progress",
                message="Synthetic worker progress before failure.",
            )
        )
        return TaskWorkerResult(
            task_id="T01",
            title="Task A",
            branch=str(task["branch"]),
            worktree_path=os.fspath(run_paths.run_dir / "worktrees" / "T01" / "repo"),
            started_at="2026-02-19T00:00:00+00:00",
            finished_at="2026-02-19T00:01:00+00:00",
            success=False,
            summary="worker failed",
            commit_hash=None,
            error="max_steps exceeded",
            report_path=".alysis/runs/x/execution/reports/T01.md",
            patch_path=".alysis/runs/x/execution/patches/T01.diff",
            log_path=".alysis/runs/x/execution/logs/T01.jsonl",
            log_pointer_path=".alysis/runs/x/execution/logs/T01.log.json",
            warnings=[],
            changed_files=[],
            verify_failed=False,
            verify_summary="verification failed (0/1)",
            verify_artifact_path=".alysis/runs/x/execution/verify/T01.txt",
            failure_reason="noop_verification_failed",
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=True,
        parallel=1,
        base_branch=None,
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=False,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        console=_null_console(),
        scope_mode="warn",
        verify_mode="warn",
        review=False,
        trace_level="compact",
        worker_runner=failing_worker_runner,
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda *_a, **_k: None,
        branch_exists_fn=lambda *_a, **_k: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 1
    trace_path = paths.execution_dir / "trace" / "swarm_trace.jsonl"
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    phases = [str(event["phase"]) for event in events]
    messages = [str(event["message"]) for event in events]
    assert "worker.error" in phases
    assert "verify.error" in phases
    assert any(
        "Already-satisfied verification failed: verification failed (0/1)" in message
        for message in messages
    )
    assert any("Swarm completed with exit code 1." in message for message in messages)
    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[task["id"]] == "failed"


def test_run_swarm_flushes_trace_artifact_on_review_failure(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    def fake_review_runner(**kwargs):  # type: ignore[no-untyped-def]
        task_id = str((kwargs.get("task") or {}).get("id") or "T01")
        review_dir = paths.execution_dir / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        return ReviewOutcome(
            task_id=task_id,
            approved=False,
            confidence="high",
            summary="changes requested",
            blocking_issues_count=1,
            non_blocking_issues_count=0,
            json_path=review_dir / f"{task_id}.json",
            markdown_path=review_dir / f"{task_id}.md",
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=True,
        parallel=1,
        base_branch=None,
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=False,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        console=_null_console(),
        scope_mode="warn",
        verify_mode="off",
        review=True,
        trace_level="compact",
        worker_runner=lambda **kwargs: _ok_worker_result(
            run_dir=kwargs["run_paths"].run_dir,
            task_id=str(kwargs["task"]["id"]),
            branch=str(kwargs["task"]["branch"]),
        ),
        review_runner=fake_review_runner,
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda *_a, **_k: None,
        branch_exists_fn=lambda *_a, **_k: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 1
    trace_path = paths.execution_dir / "trace" / "swarm_trace.jsonl"
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    phases = [str(event["phase"]) for event in events]
    assert "review.error" in phases
    assert any("Swarm completed with exit code 1." in str(event["message"]) for event in events)


def test_swarm_review_receives_structured_verification_payload_from_worker_output(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    verify_payload = {
        "summary": "verification passed (1/1)",
        "all_passed": True,
        "failed_commands": [],
        "command_results": [
            {
                "command": "pytest -q",
                "effective_command": "pytest -q",
                "exit_code": 0,
                "ok": True,
                "real_execution": True,
                "fallback_used": False,
                "output_preview": "1 passed\n",
            }
        ],
        "fallback_used": False,
        "fallback_count": 0,
    }
    captured: dict[str, object] = {}

    def worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        task = kwargs["task"]
        task_id = str(task["id"])
        branch = str(task["branch"])
        return TaskWorkerResult(
            task_id=task_id,
            title=f"title-{task_id}",
            branch=branch,
            worktree_path=os.fspath(paths.run_dir / "worktrees" / task_id / "repo"),
            started_at="2026-02-19T00:00:00+00:00",
            finished_at="2026-02-19T00:01:00+00:00",
            success=True,
            summary="ok",
            commit_hash=f"commit-{task_id}",
            error=None,
            report_path=f".alysis/runs/x/execution/reports/{task_id}.md",
            patch_path=f".alysis/runs/x/execution/patches/{task_id}.diff",
            log_path=f".alysis/runs/x/execution/logs/{task_id}.jsonl",
            log_pointer_path=f".alysis/runs/x/execution/logs/{task_id}.log.json",
            warnings=[],
            changed_files=["src/example.py"],
            verify_failed=False,
            verify_summary=None,
            verify_artifact_path=f".alysis/runs/x/execution/verify/{task_id}.txt",
            verify_payload=verify_payload,
        )

    def fake_review_runner(**kwargs):  # type: ignore[no-untyped-def]
        captured["verification"] = kwargs.get("verification_payload_override")
        task_id = str((kwargs.get("task") or {}).get("id") or "T01")
        review_dir = paths.execution_dir / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        return ReviewOutcome(
            task_id=task_id,
            approved=False,
            confidence="high",
            summary="changes requested",
            blocking_issues_count=1,
            non_blocking_issues_count=0,
            json_path=review_dir / f"{task_id}.json",
            markdown_path=review_dir / f"{task_id}.md",
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=True,
        parallel=1,
        base_branch=None,
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=False,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        console=_null_console(),
        scope_mode="warn",
        verify_mode="warn",
        review=True,
        worker_runner=worker_runner,
        review_runner=fake_review_runner,
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda *_a, **_k: None,
        branch_exists_fn=lambda *_a, **_k: True,
        current_branch_fn=lambda _root: "main",
    )

    assert code == 1
    verification = captured["verification"]
    assert isinstance(verification, dict)
    assert verification["summary"] == "verification passed (1/1)"
    command_results = verification["command_results"]
    assert isinstance(command_results, list)
    assert command_results[0]["command"] == "pytest -q"
    assert command_results[0]["ok"] is True

    worker_result = _load_json(paths.execution_dir / "worker_results" / "T01.json")
    assert worker_result["verify_summary"] is None
    assert worker_result["verify_payload"]["summary"] == "verification passed (1/1)"
    assert worker_result["verify_payload"]["command_results"][0]["command"] == "pytest -q"


def test_swarm_review_rejection_keeps_structured_capture_artifacts_unpromoted(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src/example.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    def worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        run_paths = kwargs["run_paths"]
        task_obj = kwargs["task"]
        artifact_dir = _persist_worker_capture_artifact(paths=run_paths, task=task_obj)
        attempt_entry = _persist_worker_task_attempt(paths=run_paths, task=task_obj)
        rel_dir = artifact_dir.resolve().relative_to(run_paths.root.resolve()).as_posix()
        return _ok_worker_result(
            run_dir=run_paths.run_dir,
            task_id=str(task_obj["id"]),
            branch=str(task_obj["branch"]),
            knowledge_capture_artifact_dir=rel_dir,
            task_attempt_entry_id=attempt_entry.id,
            task_attempt_knowledge_file_path=(
                attempt_entry.file_path.resolve().relative_to(run_paths.root.resolve()).as_posix()
                if attempt_entry.file_path is not None
                else None
            ),
        )

    def fake_review_runner(**kwargs):  # type: ignore[no-untyped-def]
        task_id = str((kwargs.get("task") or {}).get("id") or "T01")
        review_dir = paths.execution_dir / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        return ReviewOutcome(
            task_id=task_id,
            approved=False,
            confidence="high",
            summary="changes requested",
            blocking_issues_count=1,
            non_blocking_issues_count=0,
            json_path=review_dir / f"{task_id}.json",
            markdown_path=review_dir / f"{task_id}.md",
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=True,
        parallel=1,
        base_branch=None,
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=False,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        console=_null_console(),
        scope_mode="warn",
        verify_mode="off",
        review=True,
        worker_runner=worker_runner,
        review_runner=fake_review_runner,
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda *_a, **_k: None,
        branch_exists_fn=lambda *_a, **_k: True,
        current_branch_fn=lambda _root: "main",
    )

    assert code == 1
    final_plan = load_plan(paths)
    assert final_plan["tasks"][0]["status"] == "changes_requested"
    capture_dir = paths.execution_knowledge_capture_dir / str(task["id"]) / "attempt_001"
    promotion_payload = _load_json(capture_dir / "promotion.json")
    assert promotion_payload["promotion_attempted"] is False
    assert promotion_payload["promotion_succeeded"] is False
    assert (
        promotion_payload["promotion_skipped_reason"]
        == "worker result was not accepted because review requested changes"
    )
    assert list((paths.knowledge_facts_dir / str(task["id"])).glob("*.md")) == []
    assert list((paths.knowledge_decisions_dir / str(task["id"])).glob("*.md")) == []
    index = load_knowledge_index(paths, rebuild=True)
    attempt_entry = next(
        entry
        for entry in index.entries
        if entry.kind == "task_attempt" and entry.result == "success"
    )
    assert attempt_entry.status == "pending"
    assert attempt_entry.effective_status == "rejected"


def test_swarm_merge_acceptance_promotes_structured_capture_exactly_once(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src/example.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    def worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        run_paths = kwargs["run_paths"]
        task_obj = kwargs["task"]
        artifact_dir = _persist_worker_capture_artifact(paths=run_paths, task=task_obj)
        attempt_entry = _persist_worker_task_attempt(paths=run_paths, task=task_obj)
        rel_dir = artifact_dir.resolve().relative_to(run_paths.root.resolve()).as_posix()
        return _ok_worker_result(
            run_dir=run_paths.run_dir,
            task_id=str(task_obj["id"]),
            branch=str(task_obj["branch"]),
            knowledge_capture_artifact_dir=rel_dir,
            task_attempt_entry_id=attempt_entry.id,
            task_attempt_knowledge_file_path=(
                attempt_entry.file_path.resolve().relative_to(run_paths.root.resolve()).as_posix()
                if attempt_entry.file_path is not None
                else None
            ),
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=True,
        parallel=1,
        base_branch=None,
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=False,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        console=_null_console(),
        scope_mode="warn",
        verify_mode="off",
        integration_mode="off",
        review=False,
        worker_runner=worker_runner,
        merge_runner=lambda *_a, **_k: "merge-123",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda *_a, **_k: None,
        branch_exists_fn=lambda *_a, **_k: True,
        current_branch_fn=lambda _root: "main",
    )

    assert code == 0
    final_plan = load_plan(paths)
    assert final_plan["tasks"][0]["status"] == "done"
    capture_dir = paths.execution_knowledge_capture_dir / str(task["id"]) / "attempt_001"
    promotion_payload = _load_json(capture_dir / "promotion.json")
    assert promotion_payload["promotion_attempted"] is True
    assert promotion_payload["promotion_succeeded"] is True
    assert len(promotion_payload["fact_entry_ids"]) == 1
    assert len(promotion_payload["decision_entry_ids"]) == 1
    assert len(list((paths.knowledge_facts_dir / str(task["id"])).glob("*.md"))) == 1
    assert len(list((paths.knowledge_decisions_dir / str(task["id"])).glob("*.md"))) == 1
    index = load_knowledge_index(paths, rebuild=True)
    attempt_entry = next(
        entry
        for entry in index.entries
        if entry.kind == "task_attempt" and entry.result == "success"
    )
    assert attempt_entry.status == "pending"
    assert attempt_entry.effective_status == "accepted"


def test_swarm_accepts_verified_noop_without_review_or_merge(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_head(repo)
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src/example.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    calls = {"review": 0, "merge": 0, "remove": 0, "delete": 0}

    def worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        run_paths = kwargs["run_paths"]
        task_obj = kwargs["task"]
        (run_paths.run_dir / "worktrees" / str(task_obj["id"]) / "repo").mkdir(
            parents=True,
            exist_ok=True,
        )
        artifact_dir = _persist_worker_capture_artifact(paths=run_paths, task=task_obj)
        attempt_entry = _persist_worker_task_attempt(paths=run_paths, task=task_obj)
        rel_dir = artifact_dir.resolve().relative_to(run_paths.root.resolve()).as_posix()
        return _ok_worker_result(
            run_dir=run_paths.run_dir,
            task_id=str(task_obj["id"]),
            branch=str(task_obj["branch"]),
            knowledge_capture_artifact_dir=rel_dir,
            task_attempt_entry_id=attempt_entry.id,
            task_attempt_knowledge_file_path=(
                attempt_entry.file_path.resolve().relative_to(run_paths.root.resolve()).as_posix()
                if attempt_entry.file_path is not None
                else None
            ),
            commit_hash=None,
            changed_files=[],
            result_kind="success_noop",
            noop_reason="already_satisfied",
        )

    def fake_review_runner(**_kwargs):  # type: ignore[no-untyped-def]
        calls["review"] += 1
        raise AssertionError("review should not run for accepted no-op worker results")

    def fake_merge_runner(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        calls["merge"] += 1
        raise AssertionError("merge should not run for accepted no-op worker results")

    def fake_remove_worktree_fn(**_kwargs):  # type: ignore[no-untyped-def]
        calls["remove"] += 1

    def fake_delete_branch_fn(_root: Path, _branch: str) -> None:
        calls["delete"] += 1

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=True,
        parallel=1,
        base_branch=None,
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=False,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        console=_null_console(),
        scope_mode="warn",
        verify_mode="warn",
        review=True,
        worker_runner=worker_runner,
        review_runner=fake_review_runner,
        merge_runner=fake_merge_runner,
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=fake_remove_worktree_fn,
        delete_branch_fn=fake_delete_branch_fn,
        branch_exists_fn=lambda *_a, **_k: True,
        current_branch_fn=lambda _root: "main",
    )

    assert code == 0
    assert calls["review"] == 0
    assert calls["merge"] == 0
    assert calls["remove"] == 1
    assert calls["delete"] == 1

    final_plan = load_plan(paths)
    assert final_plan["tasks"][0]["status"] == "already_satisfied"
    assert "merge_commit_hash" not in final_plan["tasks"][0]

    worker_result = _load_json(paths.execution_dir / "worker_results" / f"{task['id']}.json")
    assert worker_result["result_kind"] == "success_noop"
    assert worker_result["noop_success"] is True
    assert worker_result["noop_reason"] == "already_satisfied"
    assert worker_result["commit_hash"] is None

    merge_result = _load_json(paths.execution_dir / "merge_results" / f"{task['id']}.json")
    assert merge_result["success"] is True
    assert merge_result["action"] == "noop"
    assert merge_result["merge_commit_hash"] is None
    assert merge_result["backend_name"] == "git_worktree"

    summary = (paths.execution_dir / "swarm_summary.md").read_text(encoding="utf-8")
    assert "already-satisfied no-op" in summary
    assert "no merge required" in summary


def test_swarm_suppresses_scope_irrelevant_generic_preset_for_static_noop_worker(
    tmp_path: Path,
) -> None:
    # A generic preset like ``pytest -q`` is only trusted once a real test surface is
    # confirmed. For a task scoped to static files (index.html/style.css) in a repo
    # with no discoverable Python test surface, the worker receives no verify command
    # and the run records the first-class, non-failing
    # ``task_refinement.no_authoritative_commands`` selection instead of forcing an
    # empty pytest run. The worker's resolved contract (cfg.verify_commands) reflects
    # that suppression.
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_head(repo)
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Create static site files",
        estimated_files=["index.html", "style.css"],
        branch="feat/t01-static",
    )
    task["write_scope"] = ["index.html", "style.css"]
    save_plan(paths, plan)
    captured: dict[str, object] = {}

    def worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        captured["verify_commands"] = list(kwargs["verify_commands"])
        captured["cfg_verify_commands"] = list(kwargs["cfg"].verify_commands)
        selection = kwargs["verify_command_selection"]
        captured["verify_command_selection_source"] = (
            selection.source if selection is not None else None
        )
        t = kwargs["task"]
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(t["id"]),
            branch=str(t["branch"]),
            commit_hash=None,
            changed_files=[],
            result_kind="success_noop",
            noop_reason="already_satisfied",
            verify_payload={
                "commands": list(kwargs["verify_commands"]),
                "all_passed": True,
                "command_results": [],
            },
            verify_command_source=str(captured["verify_command_selection_source"] or ""),
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model", verify_commands=["pytest -q"]),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=True,
        parallel=1,
        base_branch=None,
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=False,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        console=_null_console(),
        scope_mode="warn",
        verify_mode="warn",
        integration_mode="off",
        review=False,
        worker_runner=worker_runner,
        merge_runner=lambda *_a, **_kwargs: (_ for _ in ()).throw(
            AssertionError("accepted no-op should not merge")
        ),
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda *_a, **_kwargs: None,
        branch_exists_fn=lambda *_a, **_kwargs: True,
        current_branch_fn=lambda _root: "main",
    )

    assert code == 0
    assert captured["verify_commands"] == []
    assert captured["cfg_verify_commands"] == []
    assert (
        captured["verify_command_selection_source"] == "task_refinement.no_authoritative_commands"
    )
    final_plan = load_plan(paths)
    assert final_plan["tasks"][0]["status"] == "already_satisfied"


def test_swarm_passes_authoritative_config_verify_commands_to_worker(
    tmp_path: Path,
) -> None:
    # An authoritative, bespoke verify command (``config.verify_commands``) is never
    # refined away by task/repo heuristics: it reaches the worker verbatim even for a
    # scope-irrelevant static task. This guards the plumbing that hands configured
    # verify commands through to the worker.
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_head(repo)
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Create static site files",
        estimated_files=["index.html", "style.css"],
        branch="feat/t01-static",
    )
    task["write_scope"] = ["index.html", "style.css"]
    save_plan(paths, plan)
    captured: dict[str, object] = {}

    def worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        captured["verify_commands"] = list(kwargs["verify_commands"])
        captured["cfg_verify_commands"] = list(kwargs["cfg"].verify_commands)
        selection = kwargs["verify_command_selection"]
        captured["verify_command_selection_source"] = (
            selection.source if selection is not None else None
        )
        t = kwargs["task"]
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(t["id"]),
            branch=str(t["branch"]),
            commit_hash=None,
            changed_files=[],
            result_kind="success_noop",
            noop_reason="already_satisfied",
            verify_payload={
                "commands": list(kwargs["verify_commands"]),
                "all_passed": True,
                "command_results": [],
            },
            verify_command_source=str(captured["verify_command_selection_source"] or ""),
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model", verify_commands=["make verify"]),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=True,
        parallel=1,
        base_branch=None,
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=False,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        console=_null_console(),
        scope_mode="warn",
        verify_mode="warn",
        integration_mode="off",
        review=False,
        worker_runner=worker_runner,
        merge_runner=lambda *_a, **_kwargs: (_ for _ in ()).throw(
            AssertionError("accepted no-op should not merge")
        ),
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda *_a, **_kwargs: None,
        branch_exists_fn=lambda *_a, **_kwargs: True,
        current_branch_fn=lambda _root: "main",
    )

    assert code == 0
    assert captured["verify_commands"] == ["make verify"]
    assert captured["cfg_verify_commands"] == ["make verify"]
    assert captured["verify_command_selection_source"] == "config.verify_commands"
    final_plan = load_plan(paths)
    assert final_plan["tasks"][0]["status"] == "already_satisfied"


def test_swarm_rejects_salvaged_agent_exception_worker_result(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src/example.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    def worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        run_paths = kwargs["run_paths"]
        task_obj = kwargs["task"]
        artifact_dir = _persist_worker_capture_artifact(paths=run_paths, task=task_obj)
        attempt_entry = _persist_worker_task_attempt(paths=run_paths, task=task_obj)
        rel_dir = artifact_dir.resolve().relative_to(run_paths.root.resolve()).as_posix()
        return _ok_worker_result(
            run_dir=run_paths.run_dir,
            task_id=str(task_obj["id"]),
            branch=str(task_obj["branch"]),
            knowledge_capture_artifact_dir=rel_dir,
            task_attempt_entry_id=attempt_entry.id,
            task_attempt_knowledge_file_path=(
                attempt_entry.file_path.resolve().relative_to(run_paths.root.resolve()).as_posix()
                if attempt_entry.file_path is not None
                else None
            ),
            agent_exit_code=1,
            salvaged_agent_exception=True,
            agent_exception_summary="TimeoutError: provider timeout",
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=True,
        parallel=1,
        base_branch=None,
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=False,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        console=_null_console(),
        scope_mode="warn",
        verify_mode="off",
        integration_mode="off",
        review=False,
        worker_runner=worker_runner,
        merge_runner=lambda *_a, **_k: "merge-123",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda *_a, **_k: None,
        branch_exists_fn=lambda *_a, **_k: True,
        current_branch_fn=lambda _root: "main",
    )

    assert code == 1
    final_plan = load_plan(paths)
    assert final_plan["tasks"][0]["status"] == "failed"

    worker_result = _load_json(paths.execution_dir / "worker_results" / f"{task['id']}.json")
    assert worker_result["success"] is False
    assert worker_result["agent_exit_code"] == 1
    assert worker_result["salvaged_agent_exception"] is False
    assert worker_result["salvaged_nonzero_exit"] is False
    assert worker_result["agent_exception_summary"] == "TimeoutError: provider timeout"
    assert "refusing to accept partial worker result" in worker_result["error"]

    merge_result_path = paths.execution_dir / "merge_results" / f"{task['id']}.json"
    assert not merge_result_path.exists()

    summary = (paths.execution_dir / "swarm_summary.md").read_text(encoding="utf-8")
    assert "refusing to accept partial worker result" in summary


def test_swarm_rejects_nonzero_exit_worker_result_as_verified_noop(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src/example.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    def worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        run_paths = kwargs["run_paths"]
        task_obj = kwargs["task"]
        artifact_dir = _persist_worker_capture_artifact(paths=run_paths, task=task_obj)
        attempt_entry = _persist_worker_task_attempt(paths=run_paths, task=task_obj)
        rel_dir = artifact_dir.resolve().relative_to(run_paths.root.resolve()).as_posix()
        return _ok_worker_result(
            run_dir=run_paths.run_dir,
            task_id=str(task_obj["id"]),
            branch=str(task_obj["branch"]),
            knowledge_capture_artifact_dir=rel_dir,
            task_attempt_entry_id=attempt_entry.id,
            task_attempt_knowledge_file_path=(
                attempt_entry.file_path.resolve().relative_to(run_paths.root.resolve()).as_posix()
                if attempt_entry.file_path is not None
                else None
            ),
            commit_hash=None,
            changed_files=[],
            result_kind="success_noop",
            noop_reason="already_satisfied",
            agent_exit_code=1,
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=True,
        parallel=1,
        base_branch=None,
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=False,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        console=_null_console(),
        scope_mode="warn",
        verify_mode="warn",
        review=False,
        worker_runner=worker_runner,
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda *_a, **_k: None,
        branch_exists_fn=lambda *_a, **_k: True,
        current_branch_fn=lambda _root: "main",
    )

    assert code == 1
    final_plan = load_plan(paths)
    assert final_plan["tasks"][0]["status"] == "failed"

    worker_result = _load_json(paths.execution_dir / "worker_results" / f"{task['id']}.json")
    assert worker_result["success"] is False
    assert worker_result["agent_exit_code"] == 1
    assert worker_result["result_kind"] == "failure"
    assert worker_result["noop_success"] is False
    assert worker_result["salvaged_nonzero_exit"] is False
    assert worker_result["salvaged_agent_exception"] is False
    assert "agent_exit_code=1" in worker_result["error"]

    merge_result_path = paths.execution_dir / "merge_results" / f"{task['id']}.json"
    assert not merge_result_path.exists()

    summary = (paths.execution_dir / "swarm_summary.md").read_text(encoding="utf-8")
    assert "refusing to accept partial worker result" in summary


def test_swarm_sanitizes_persisted_agent_exception_summary_artifacts(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src/example.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    secret_token = "sk-" + ("abc12345" * 4)
    secret_bearer = "Bearer " + ("tok12345" * 5)
    raw_summary = f"TimeoutError: provider timeout Authorization: {secret_token} {secret_bearer}"

    def worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        run_paths = kwargs["run_paths"]
        task_obj = kwargs["task"]
        artifact_dir = _persist_worker_capture_artifact(paths=run_paths, task=task_obj)
        attempt_entry = _persist_worker_task_attempt(paths=run_paths, task=task_obj)
        rel_dir = artifact_dir.resolve().relative_to(run_paths.root.resolve()).as_posix()
        return _ok_worker_result(
            run_dir=run_paths.run_dir,
            task_id=str(task_obj["id"]),
            branch=str(task_obj["branch"]),
            knowledge_capture_artifact_dir=rel_dir,
            task_attempt_entry_id=attempt_entry.id,
            task_attempt_knowledge_file_path=(
                attempt_entry.file_path.resolve().relative_to(run_paths.root.resolve()).as_posix()
                if attempt_entry.file_path is not None
                else None
            ),
            agent_exit_code=1,
            salvaged_agent_exception=True,
            agent_exception_summary=raw_summary,
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=True,
        parallel=1,
        base_branch=None,
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=False,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        console=_null_console(),
        scope_mode="warn",
        verify_mode="off",
        integration_mode="off",
        review=False,
        worker_runner=worker_runner,
        merge_runner=lambda *_a, **_k: "merge-123",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda *_a, **_k: None,
        branch_exists_fn=lambda *_a, **_k: True,
        current_branch_fn=lambda _root: "main",
    )

    assert code == 1
    worker_result = _load_json(paths.execution_dir / "worker_results" / f"{task['id']}.json")
    clean_summary = str(worker_result["agent_exception_summary"])
    assert secret_token not in clean_summary
    assert secret_bearer not in clean_summary
    assert "[REDACTED]" in clean_summary


def test_swarm_merge_failure_does_not_leave_task_attempt_effectively_accepted(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(plan, title="Task A", estimated_files=["src/example.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    def worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        run_paths = kwargs["run_paths"]
        task_obj = kwargs["task"]
        artifact_dir = _persist_worker_capture_artifact(paths=run_paths, task=task_obj)
        attempt_entry = _persist_worker_task_attempt(paths=run_paths, task=task_obj)
        rel_dir = artifact_dir.resolve().relative_to(run_paths.root.resolve()).as_posix()
        return _ok_worker_result(
            run_dir=run_paths.run_dir,
            task_id=str(task_obj["id"]),
            branch=str(task_obj["branch"]),
            knowledge_capture_artifact_dir=rel_dir,
            task_attempt_entry_id=attempt_entry.id,
            task_attempt_knowledge_file_path=(
                attempt_entry.file_path.resolve().relative_to(run_paths.root.resolve()).as_posix()
                if attempt_entry.file_path is not None
                else None
            ),
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=True,
        parallel=1,
        base_branch=None,
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=False,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        console=_null_console(),
        scope_mode="warn",
        verify_mode="off",
        integration_mode="off",
        review=False,
        worker_runner=worker_runner,
        merge_runner=lambda *_a, **_k: (_ for _ in ()).throw(GitOpsError("merge failed")),
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda *_a, **_k: None,
        branch_exists_fn=lambda *_a, **_k: True,
        current_branch_fn=lambda _root: "main",
    )

    assert code == 1
    index = load_knowledge_index(paths, rebuild=True)
    attempt_entry = next(
        entry
        for entry in index.entries
        if entry.kind == "task_attempt" and entry.result == "success"
    )
    assert attempt_entry.status == "pending"
    assert attempt_entry.effective_status == "rejected"


def test_run_swarm_flushes_trace_artifact_on_merge_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    monkeypatch.setattr("alysis_code.swarm_orchestrator.list_unmerged_files", lambda _root: [])

    def fake_merge_runner(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise GitOpsError("merge exploded")

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=True,
        parallel=1,
        base_branch=None,
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=False,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        console=_null_console(),
        scope_mode="warn",
        verify_mode="off",
        integration_mode="off",
        review=False,
        trace_level="compact",
        worker_runner=lambda **kwargs: _ok_worker_result(
            run_dir=kwargs["run_paths"].run_dir,
            task_id=str(kwargs["task"]["id"]),
            branch=str(kwargs["task"]["branch"]),
        ),
        merge_runner=fake_merge_runner,
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda *_a, **_k: None,
        branch_exists_fn=lambda *_a, **_k: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 1
    trace_path = paths.execution_dir / "trace" / "swarm_trace.jsonl"
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    phases = [str(event["phase"]) for event in events]
    assert "merge.error" in phases
    assert any("Swarm completed with exit code 1." in str(event["message"]) for event in events)


def test_swarm_non_dry_run_treats_empty_verify_commands_as_unavailable(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    cfg = AppConfig(model="test-model")
    cfg.verify_commands = []
    captured: dict[str, object] = {}

    def worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        captured["verify_commands"] = list(kwargs["verify_commands"])
        captured["cfg_verify_commands"] = list(kwargs["cfg"].verify_commands)
        captured["verify_command_selection"] = kwargs["verify_command_selection"]
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id="T01",
            branch="feat/t01-a",
            verify_summary="verification skipped: no authoritative commands available",
            verify_command_source="repo_scan.no_authoritative_commands",
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=cfg,
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        integration_mode="off",
        verify_mode="warn",
        verify_cmd=None,
        console=_null_console(),
        worker_runner=worker_runner,
        merge_runner=lambda *_a, **_k: "merge",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )

    assert code == 0
    assert captured["verify_commands"] == []
    assert captured["cfg_verify_commands"] == []
    selection = captured["verify_command_selection"]
    assert selection.source == "repo_scan.no_authoritative_commands"


def test_swarm_happy_path_updates_status_and_writes_artifacts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    t1 = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    t2 = add_task(plan, title="Task B", estimated_files=["src/b.py"], branch="feat/t02-b")
    save_plan(paths, plan)

    seen_in_progress: dict[str, bool] = {}

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        task = kwargs["task"]
        task_id = str(task["id"])
        current = _load_json(paths.plan_json_path)
        current_task = next(x for x in current["tasks"] if x["id"] == task_id)
        seen_in_progress[task_id] = current_task["status"] == "in_progress"
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=task_id,
            branch=str(task["branch"]),
        )

    def fake_merge_runner(_root, *, base_branch: str, task_branch: str, message: str) -> str:
        assert base_branch == "main"
        assert task_branch in {"feat/t01-a", "feat/t02-b"}
        assert message.startswith("Merge ")
        return f"merge-{task_branch}"

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=2,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        integration_mode="off",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        merge_runner=fake_merge_runner,
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 0
    assert seen_in_progress[t1["id"]] is True
    assert seen_in_progress[t2["id"]] is True

    final_plan = _load_json(paths.plan_json_path)
    statuses = {task["id"]: task["status"] for task in final_plan["tasks"]}
    assert statuses[t1["id"]] == "done"
    assert statuses[t2["id"]] == "done"

    worker_result_1 = paths.execution_dir / "worker_results" / f"{t1['id']}.json"
    worker_result_2 = paths.execution_dir / "worker_results" / f"{t2['id']}.json"
    merge_result_1 = paths.execution_dir / "merge_results" / f"{t1['id']}.json"
    merge_result_2 = paths.execution_dir / "merge_results" / f"{t2['id']}.json"
    assert worker_result_1.exists()
    assert worker_result_2.exists()
    assert merge_result_1.exists()
    assert merge_result_2.exists()
    assert _load_json(merge_result_1)["backend_name"] == "snapshot_workspace"
    assert _load_json(merge_result_2)["backend_name"] == "snapshot_workspace"


def test_swarm_summary_preserves_executed_parallel_batch_history(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    t1 = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    t2 = add_task(plan, title="Task B", estimated_files=["src/b.py"], branch="feat/t02-b")
    save_plan(paths, plan)

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        task = kwargs["task"]
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(task["id"]),
            branch=str(task["branch"]),
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=2,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        integration_mode="off",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        merge_runner=lambda *_a, **_k: "merge",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 0

    summary = (paths.execution_dir / "swarm_summary.md").read_text(encoding="utf-8")
    assert f"- Batch 1: {t1['id']}, {t2['id']}" in summary
    assert f"- `{t1['id']}`: already done" not in summary
    assert f"- `{t2['id']}`: already done" not in summary


def test_swarm_returns_nonzero_when_worker_reports_failure(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    def failing_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        t = kwargs["task"]
        return TaskWorkerResult(
            task_id=str(t["id"]),
            title=str(t["title"]),
            branch=str(t["branch"]),
            worktree_path=os.fspath(paths.run_dir / "worktrees" / str(t["id"]) / "repo"),
            started_at="2026-02-19T00:00:00+00:00",
            finished_at="2026-02-19T00:01:00+00:00",
            success=False,
            summary="worker failed",
            commit_hash="deadbeef",
            error="worker failed",
            report_path="r.md",
            patch_path="p.diff",
            log_path="l.jsonl",
            log_pointer_path="lp.json",
            warnings=[],
            changed_files=["src/a.py"],
            verify_failed=False,
            verify_summary=None,
            verify_artifact_path=None,
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="warn",
        verify_cmd=["pytest -q"],
        console=_null_console(),
        worker_runner=failing_worker_runner,
        merge_runner=lambda *_a, **_k: "merge",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 1

    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[task["id"]] == "failed"


def test_swarm_preserves_role_models_in_worker_cfg(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    captured: dict[str, str] = {}

    def fake_run_agent(**kwargs):  # type: ignore[no-untyped-def]
        cfg = kwargs["cfg"]
        captured["model"] = cfg.model
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", lambda _root: None)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["src/a.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["src/a.py"],
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/a.py"],
    )

    cfg = AppConfig(model="default-model")
    cfg.extra_fields = {"role_models": {"coding": "coding-role-model"}}

    def fake_ensure_worktree(**kwargs):  # type: ignore[no-untyped-def]
        worktree_path = Path(kwargs["worktree_repo_path"])
        worktree_path.mkdir(parents=True, exist_ok=True)

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=cfg,
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=True,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        scope_mode="warn",
        verify_mode="off",
        integration_mode="off",
        console=_null_console(),
        worker_runner=run_task_worker,
        merge_runner=lambda *_a, **_k: "merge123",
        ensure_worktree_fn=fake_ensure_worktree,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 0
    assert captured["model"] == "coding-role-model"
    final_plan = _load_json(paths.plan_json_path)
    assert final_plan["tasks"][0]["status"] == "done"


def test_swarm_merge_conflict_marks_status_and_continues_other_tasks(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    t1 = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    t2 = add_task(plan, title="Task B", estimated_files=["src/b.py"], branch="feat/t02-b")
    save_plan(paths, plan)

    merges: list[str] = []

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        task = kwargs["task"]
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(task["id"]),
            branch=str(task["branch"]),
        )

    def fake_merge_runner(_root, *, base_branch: str, task_branch: str, message: str) -> str:
        merges.append(task_branch)
        if task_branch == "feat/t01-a":
            raise GitOpsError("conflict")
        return "merge-ok"

    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.load_conflict_auto_resolve_settings",
        lambda *, cfg: ConflictAutoResolveSettings(enabled=False),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.list_unmerged_files",
        lambda _root: ["src/x.py"],
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.capture_merge_conflict_context",
        lambda _root, *, base_branch, task_branch, merge_error: {
            "base_branch": base_branch,
            "task_branch": task_branch,
            "merge_error": merge_error,
            "git_status_porcelain": "UU src/x.py",
            "unmerged_files": ["src/x.py"],
            "files": [],
        },
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.review_merge_conflict",
        lambda **_kwargs: ConflictReviewOutcome(
            review_json={
                "task_id": t1["id"],
                "confidence": "medium",
                "summary": "conflict",
                "root_cause": "same lines changed",
                "recommended_strategy": "manual_merge",
                "per_file": [],
                "next_steps": ["resolve"],
            },
            review_markdown="# Merge conflict review\n",
            skipped_reason=None,
        ),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.try_abort_merge",
        lambda _root, *, base_branch: (
            True,
            f"$ git -C {_root} merge --abort\nbase={base_branch}\n",
        ),
    )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=2,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        integration_mode="off",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        merge_runner=fake_merge_runner,
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 1
    assert set(merges) == {"feat/t01-a", "feat/t02-b"}
    assert len(merges) == 2

    final_plan = _load_json(paths.plan_json_path)
    statuses = {task["id"]: task["status"] for task in final_plan["tasks"]}
    assert statuses[t1["id"]] == "merge_conflict"
    assert statuses[t2["id"]] == "done"

    conflict_dir = paths.execution_dir / "conflicts" / t1["id"]
    assert (conflict_dir / "conflict_context.json").exists()
    assert (conflict_dir / "conflict_review.json").exists()
    assert (conflict_dir / "conflict_review.md").exists()
    assert (conflict_dir / "merge_cleanup.log").exists()

    summary = (paths.execution_dir / "swarm_summary.md").read_text(encoding="utf-8")
    assert "Review conflict report" in summary
    assert f"execution/conflicts/{t1['id']}/conflict_review.md" in summary


def test_swarm_pending_ready_merge_failure_does_not_block_remaining_schedule(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_head(repo)
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    t1 = add_task(plan, title="Ready merge task", estimated_files=["src/a.py"], branch="feat/t01-a")
    t2 = add_task(plan, title="Runnable task", estimated_files=["src/b.py"], branch="feat/t02-b")
    t1["status"] = "ready_for_merge"
    save_plan(paths, plan)

    worker_calls: list[str] = []
    merges: list[str] = []

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        task = kwargs["task"]
        task_id = str(task["id"])
        worker_calls.append(task_id)
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=task_id,
            branch=str(task["branch"]),
        )

    def fake_merge_runner(_root, *, base_branch: str, task_branch: str, message: str) -> str:
        _ = base_branch, message
        merges.append(task_branch)
        if task_branch == "feat/t01-a":
            raise GitOpsError("conflict")
        return "merge-ok"

    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.list_unmerged_files",
        lambda _root: ["src/x.py"],
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.capture_merge_conflict_context",
        lambda _root, *, base_branch, task_branch, merge_error: {
            "base_branch": base_branch,
            "task_branch": task_branch,
            "merge_error": merge_error,
            "git_status_porcelain": "UU src/x.py",
            "unmerged_files": ["src/x.py"],
            "files": [],
        },
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.review_merge_conflict",
        lambda **_kwargs: ConflictReviewOutcome(
            review_json={
                "task_id": t1["id"],
                "confidence": "medium",
                "summary": "conflict",
                "root_cause": "same lines changed",
                "recommended_strategy": "manual_merge",
                "per_file": [],
                "next_steps": ["resolve"],
            },
            review_markdown="# Merge conflict review\n",
            skipped_reason=None,
        ),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.try_abort_merge",
        lambda _root, *, base_branch: (
            True,
            f"$ git -C {_root} merge --abort\nbase={base_branch}\n",
        ),
    )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        integration_mode="off",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        merge_runner=fake_merge_runner,
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 1
    assert worker_calls == [t2["id"]]
    assert set(merges) == {"feat/t01-a", "feat/t02-b"}

    final_plan = _load_json(paths.plan_json_path)
    statuses = {task["id"]: task["status"] for task in final_plan["tasks"]}
    assert statuses[t1["id"]] == "merge_conflict"
    assert statuses[t2["id"]] == "done"


def test_swarm_merge_conflict_review_artifact_records_retry_recovery(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)
    calls = {"count": 0}

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        t = kwargs["task"]
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(t["id"]),
            branch=str(t["branch"]),
        )

    def fake_merge_runner(_root, *, base_branch: str, task_branch: str, message: str) -> str:
        raise GitOpsError("conflict")

    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.list_unmerged_files",
        lambda _root: ["src/x.py"],
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.capture_merge_conflict_context",
        lambda _root, *, base_branch, task_branch, merge_error: {
            "base_branch": base_branch,
            "task_branch": task_branch,
            "merge_error": merge_error,
            "git_status_porcelain": "UU src/x.py",
            "unmerged_files": ["src/x.py"],
            "files": [],
        },
    )

    payload = {
        "task_id": task["id"],
        "confidence": "medium",
        "summary": "conflict",
        "root_cause": "same lines changed",
        "recommended_strategy": "manual_merge",
        "per_file": [],
        "next_steps": ["resolve"],
    }

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def chat(self, **_kwargs):  # type: ignore[no-untyped-def]
            calls["count"] += 1
            if calls["count"] == 1:
                raise LLMError("LLM request failed: ReadTimeout")
            return type("Resp", (), {"content": json.dumps(payload)})()

    monkeypatch.setattr(
        "alysis_code.merge_conflict_reviewer.OpenAICompatClient",
        FakeClient,
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.try_abort_merge",
        lambda _root, *, base_branch: (
            True,
            f"$ git -C {_root} merge --abort\nbase={base_branch}\n",
        ),
    )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        integration_mode="off",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        merge_runner=fake_merge_runner,
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )

    assert code == 1
    assert calls["count"] == 2
    conflict_dir = paths.execution_dir / "conflicts" / task["id"]
    review_md = (conflict_dir / "conflict_review.md").read_text(encoding="utf-8")
    review_json = json.loads((conflict_dir / "conflict_review.json").read_text(encoding="utf-8"))
    assert "Request Retries: 1 transient retry before successful review." in review_md
    assert review_json["task_id"] == task["id"]


def test_swarm_merge_conflict_review_artifact_truthfully_labels_final_failure_after_retry(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)
    calls = {"count": 0}

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        t = kwargs["task"]
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(t["id"]),
            branch=str(t["branch"]),
        )

    def fake_merge_runner(_root, *, base_branch: str, task_branch: str, message: str) -> str:
        raise GitOpsError("conflict")

    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.list_unmerged_files",
        lambda _root: ["src/x.py"],
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.capture_merge_conflict_context",
        lambda _root, *, base_branch, task_branch, merge_error: {
            "base_branch": base_branch,
            "task_branch": task_branch,
            "merge_error": merge_error,
            "git_status_porcelain": "UU src/x.py",
            "unmerged_files": ["src/x.py"],
            "files": [],
        },
    )

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def chat(self, **_kwargs):  # type: ignore[no-untyped-def]
            calls["count"] += 1
            if calls["count"] == 1:
                raise LLMError("LLM request failed: ReadTimeout")
            return type("Resp", (), {"content": "not-json"})()

    monkeypatch.setattr(
        "alysis_code.merge_conflict_reviewer.OpenAICompatClient",
        FakeClient,
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.try_abort_merge",
        lambda _root, *, base_branch: (
            True,
            f"$ git -C {_root} merge --abort\nbase={base_branch}\n",
        ),
    )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        integration_mode="off",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        merge_runner=fake_merge_runner,
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )

    assert code == 1
    assert calls["count"] == 2
    conflict_dir = paths.execution_dir / "conflicts" / task["id"]
    review_md = (conflict_dir / "conflict_review.md").read_text(encoding="utf-8")
    assert "Request Retries: 1 transient retry before final review failure." in review_md
    assert "exhausted before review was skipped" not in review_md
    assert (conflict_dir / "conflict_review.json").exists() is False


def test_swarm_merge_failure_without_unmerged_marks_failed(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        t = kwargs["task"]
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(t["id"]),
            branch=str(t["branch"]),
        )

    def fake_merge_runner(_root, *, base_branch: str, task_branch: str, message: str) -> str:
        raise GitOpsError("failed to checkout base")

    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.list_unmerged_files",
        lambda _root: [],
    )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        integration_mode="off",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        merge_runner=fake_merge_runner,
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 1
    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[task["id"]] == "failed"
    conflict_dir = paths.execution_dir / "conflicts" / task["id"]
    assert conflict_dir.exists() is False


def test_swarm_worktree_setup_failure_marks_failed_and_continues(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_head(repo)
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    t1 = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    t2 = add_task(plan, title="Task B", estimated_files=["src/b.py"], branch="feat/t02-b")
    save_plan(paths, plan)

    worker_calls: list[str] = []
    merge_calls: list[str] = []

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        task = kwargs["task"]
        task_id = str(task["id"])
        worker_calls.append(task_id)
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=task_id,
            branch=str(task["branch"]),
        )

    def fake_merge_runner(_root, *, base_branch: str, task_branch: str, message: str) -> str:
        _ = base_branch, message
        if base_branch == "main":
            merge_calls.append(task_branch)
        return f"merge-{task_branch}"

    def fake_ensure_worktree(**kwargs):  # type: ignore[no-untyped-def]
        branch = str(kwargs.get("branch") or "")
        if branch == "feat/t01-a":
            raise GitOpsError("branch conflict")

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode="off",
        verify_cmd=None,
        console=_null_console(),
        worker_runner=fake_worker_runner,
        integration_runner=lambda **kwargs: _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=list(kwargs["merged_task_ids"]),
            passed=True,
            summary="integration passed",
            phase=str(kwargs.get("phase") or "post_merge"),
        ),
        merge_runner=fake_merge_runner,
        ensure_worktree_fn=fake_ensure_worktree,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 1
    assert worker_calls == [t2["id"]]
    assert merge_calls == ["feat/t02-b"]

    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[t1["id"]] == "failed"
    assert statuses[t2["id"]] == "done"
    failed_task = next(entry for entry in final_plan["tasks"] if entry["id"] == t1["id"])
    assert "worktree setup failed:" in str(failed_task.get("last_error") or "")


def test_swarm_merge_conflict_auto_resolve_success_marks_done(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        t = kwargs["task"]
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(t["id"]),
            branch=str(t["branch"]),
        )

    def fake_merge_runner(_root, *, base_branch: str, task_branch: str, message: str) -> str:
        raise GitOpsError("conflict")

    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.list_unmerged_files",
        lambda _root: ["src/x.py"],
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.capture_merge_conflict_context",
        lambda _root, *, base_branch, task_branch, merge_error: {
            "base_branch": base_branch,
            "task_branch": task_branch,
            "merge_error": merge_error,
            "git_status_porcelain": "UU src/x.py",
            "unmerged_files": ["src/x.py"],
            "files": [],
        },
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.review_merge_conflict",
        lambda **_kwargs: ConflictReviewOutcome(
            review_json=None,
            review_markdown="# Merge conflict review\n",
            skipped_reason="missing key",
        ),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.try_abort_merge",
        lambda _root, *, base_branch: (
            True,
            f"$ git -C {_root} merge --abort\nbase={base_branch}\n",
        ),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.load_conflict_auto_resolve_settings",
        lambda *, cfg: ConflictAutoResolveSettings(
            enabled=True,
            verify_mode="strict",
            max_attempts=1,
        ),
    )

    def fake_auto_resolve(**kwargs) -> AutoResolveOutcome:  # type: ignore[no-untyped-def]
        run_paths = kwargs["paths"]
        task_id = kwargs["task"]["id"]
        conflict_dir = run_paths.execution_dir / "conflicts" / task_id
        conflict_dir.mkdir(parents=True, exist_ok=True)
        report_path = conflict_dir / "auto_resolve_report.md"
        report_path.write_text("# auto resolve\n", encoding="utf-8")
        patch_path = conflict_dir / "auto_resolve_patch.diff"
        patch_path.write_text("patch\n", encoding="utf-8")
        result_path = conflict_dir / "auto_resolve_result.json"
        result_path.write_text('{"success": true}\n', encoding="utf-8")
        return AutoResolveOutcome(
            success=True,
            task_id=str(task_id),
            conflict_branch=f"conflict/{str(task_id).lower()}",
            worktree_repo_path=run_paths.run_dir / "conflict_worktrees" / str(task_id) / "repo",
            result_json_path=result_path,
            report_path=report_path,
            patch_path=patch_path,
            merge_commit_hash="automerge123",
            verify_summary="verification passed (1/1)",
            warnings=[],
            error=None,
        )

    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.attempt_auto_resolve_conflict",
        fake_auto_resolve,
    )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        integration_mode="off",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        merge_runner=fake_merge_runner,
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 0

    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[task["id"]] == "done"
    assert int(final_plan["tasks"][0].get("conflict_attempts", 0)) == 1


def test_swarm_merge_conflict_auto_resolve_failure_stays_merge_conflict(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        t = kwargs["task"]
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(t["id"]),
            branch=str(t["branch"]),
        )

    def fake_merge_runner(_root, *, base_branch: str, task_branch: str, message: str) -> str:
        raise GitOpsError("conflict")

    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.list_unmerged_files",
        lambda _root: ["src/x.py"],
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.capture_merge_conflict_context",
        lambda _root, *, base_branch, task_branch, merge_error: {
            "base_branch": base_branch,
            "task_branch": task_branch,
            "merge_error": merge_error,
            "git_status_porcelain": "UU src/x.py",
            "unmerged_files": ["src/x.py"],
            "files": [],
        },
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.review_merge_conflict",
        lambda **_kwargs: ConflictReviewOutcome(
            review_json=None,
            review_markdown="# Merge conflict review\n",
            skipped_reason="missing key",
        ),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.try_abort_merge",
        lambda _root, *, base_branch: (
            True,
            f"$ git -C {_root} merge --abort\nbase={base_branch}\n",
        ),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.load_conflict_auto_resolve_settings",
        lambda *, cfg: ConflictAutoResolveSettings(
            enabled=True,
            verify_mode="strict",
            max_attempts=1,
        ),
    )

    def fake_auto_resolve(**kwargs) -> AutoResolveOutcome:  # type: ignore[no-untyped-def]
        run_paths = kwargs["paths"]
        task_id = kwargs["task"]["id"]
        conflict_dir = run_paths.execution_dir / "conflicts" / task_id
        conflict_dir.mkdir(parents=True, exist_ok=True)
        report_path = conflict_dir / "auto_resolve_report.md"
        report_path.write_text("# auto resolve\n", encoding="utf-8")
        patch_path = conflict_dir / "auto_resolve_patch.diff"
        patch_path.write_text("patch\n", encoding="utf-8")
        result_path = conflict_dir / "auto_resolve_result.json"
        result_path.write_text('{"success": false}\n', encoding="utf-8")
        return AutoResolveOutcome(
            success=False,
            task_id=str(task_id),
            conflict_branch=f"conflict/{str(task_id).lower()}",
            worktree_repo_path=run_paths.run_dir / "conflict_worktrees" / str(task_id) / "repo",
            result_json_path=result_path,
            report_path=report_path,
            patch_path=patch_path,
            merge_commit_hash=None,
            verify_summary="verification failed (0/1)",
            warnings=[],
            error="strict conflict verify failed",
        )

    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.attempt_auto_resolve_conflict",
        fake_auto_resolve,
    )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        integration_mode="off",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        merge_runner=fake_merge_runner,
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 1
    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[task["id"]] == "merge_conflict"


def test_swarm_resume_skips_done_merges_ready_and_retries_failed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_head(repo)
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    t1 = add_task(plan, title="Done", estimated_files=["src/a.py"], branch="feat/t01-a")
    t2 = add_task(plan, title="Ready", estimated_files=["src/b.py"], branch="feat/t02-b")
    t3 = add_task(plan, title="Failed", estimated_files=["src/c.py"], branch="feat/t03-c")
    t1["status"] = "done"
    t2["status"] = "ready_for_merge"
    t3["status"] = "failed"
    save_plan(paths, plan)

    worker_calls: list[str] = []
    merge_calls: list[str] = []

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        task = kwargs["task"]
        task_id = str(task["id"])
        worker_calls.append(task_id)
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=task_id,
            branch=str(task["branch"]),
        )

    def fake_merge_runner(_root, *, base_branch: str, task_branch: str, message: str) -> str:
        merge_calls.append(task_branch)
        return f"merge-{task_branch}"

    code_no_retry = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=2,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        integration_mode="off",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        merge_runner=fake_merge_runner,
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code_no_retry == 1
    assert worker_calls == []
    assert "feat/t02-b" in merge_calls
    assert "feat/t03-c" not in merge_calls

    worker_calls.clear()
    merge_calls.clear()
    plan_retry = _load_json(paths.plan_json_path)
    code_retry = run_swarm(
        paths=paths,
        plan=plan_retry,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=2,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=True,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        integration_mode="off",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        merge_runner=fake_merge_runner,
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code_retry == 0


def test_swarm_retry_failed_blocks_when_previous_failed_cleanup_is_incomplete(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_head(repo)
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Retry me", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    task_id = str(task["id"])
    branch = str(task["branch"])
    worker_calls = 0

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal worker_calls
        worker_calls += 1
        worktree = Path(kwargs["worktree_repo_path"])
        (worktree / "progress.txt").write_text("committed progress\n", encoding="utf-8")
        subprocess.run(["git", "add", "progress.txt"], cwd=worktree, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test User",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "progress",
            ],
            cwd=worktree,
            check=True,
        )
        commit_hash = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return _worker_result_for_path(
            run_paths=paths,
            task_id=task_id,
            branch=branch,
            worktree_path=worktree,
            success=False,
            summary="worker failed",
            commit_hash=commit_hash,
            error="worker failed",
            changed_files=["progress.txt"],
        )

    def fail_remove_worktree(**_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated remove failure")

    code_first = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=False,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        console=_null_console(),
        worker_runner=fake_worker_runner,
        merge_runner=lambda *_a, **_k: "merge-feat/t01-a",
        remove_worktree_fn=fail_remove_worktree,
    )
    assert code_first == 1
    marker_path = paths.run_dir / "worktrees" / task_id / "failed_cleanup.json"
    assert marker_path.exists()

    plan_retry = _load_json(paths.plan_json_path)
    code_retry = run_swarm(
        paths=paths,
        plan=plan_retry,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=False,
        retry_failed=True,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        console=_null_console(),
        worker_runner=fake_worker_runner,
        merge_runner=lambda *_a, **_k: "merge-feat/t01-a",
        remove_worktree_fn=fail_remove_worktree,
    )

    assert code_retry == 1
    assert worker_calls == 1
    final_plan = _load_json(paths.plan_json_path)
    failed_task = next(entry for entry in final_plan["tasks"] if entry["id"] == task_id)
    assert "worktree setup failed:" in str(failed_task.get("last_error") or "")
    assert "previous failed task cleanup left unresolved git-worktree state" in str(
        failed_task.get("last_error") or ""
    )


def test_swarm_summary_keeps_preexisting_done_tasks_but_not_executed_done_tasks(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    t1 = add_task(plan, title="Already done", estimated_files=["src/a.py"], branch="feat/t01-a")
    t2 = add_task(plan, title="Runnable", estimated_files=["src/b.py"], branch="feat/t02-b")
    t1["status"] = "done"
    save_plan(paths, plan)

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        task = kwargs["task"]
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(task["id"]),
            branch=str(task["branch"]),
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=2,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        integration_mode="off",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        integration_runner=lambda **kwargs: _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=list(kwargs["merged_task_ids"]),
            passed=True,
            summary="integration passed",
            phase=str(kwargs.get("phase") or "post_merge"),
        ),
        merge_runner=lambda *_a, **_k: "merge",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 0

    summary = (paths.execution_dir / "swarm_summary.md").read_text(encoding="utf-8")
    assert f"- Batch 1: {t2['id']}" in summary
    assert f"- `{t1['id']}`: already done" in summary
    assert f"- `{t2['id']}`: already done" not in summary


def test_swarm_retry_failed_recreates_dirty_git_worktree_without_contamination(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_head(repo)
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Retry me", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    task_id = str(task["id"])
    branch = str(task["branch"])
    preserved_commit: str | None = None
    stale_checks: list[bool] = []
    worker_calls = 0

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal preserved_commit, worker_calls
        worker_calls += 1
        worktree = Path(kwargs["worktree_repo_path"])
        stale_checks.append((worktree / "stale.txt").exists())
        if worker_calls == 1:
            (worktree / "progress.txt").write_text("committed progress\n", encoding="utf-8")
            subprocess.run(["git", "add", "progress.txt"], cwd=worktree, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test User",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "-m",
                    "progress",
                ],
                cwd=worktree,
                check=True,
            )
            preserved_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            (worktree / "stale.txt").write_text("leftover\n", encoding="utf-8")
            return _worker_result_for_path(
                run_paths=paths,
                task_id=task_id,
                branch=branch,
                worktree_path=worktree,
                success=False,
                summary="worker failed",
                commit_hash=preserved_commit,
                error="worker failed",
                changed_files=["progress.txt"],
            )

        assert preserved_commit is not None
        current_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert current_head == preserved_commit
        return _worker_result_for_path(
            run_paths=paths,
            task_id=task_id,
            branch=branch,
            worktree_path=worktree,
            success=True,
            summary="ok",
            commit_hash=preserved_commit,
            changed_files=["progress.txt"],
        )

    code_first = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        console=_null_console(),
        worker_runner=fake_worker_runner,
        merge_runner=lambda *_a, **_k: "merge-feat/t01-a",
    )
    assert code_first == 1

    plan_retry = _load_json(paths.plan_json_path)
    code_retry = run_swarm(
        paths=paths,
        plan=plan_retry,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=True,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        console=_null_console(),
        worker_runner=fake_worker_runner,
        merge_runner=lambda *_a, **_k: "merge-feat/t01-a",
    )
    assert code_retry == 0
    assert stale_checks == [False, False]


def test_swarm_failed_attempt_cleanup_with_keep_worktrees_false_recreates_from_clean_base(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_head(repo)
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Retry me", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    task_id = str(task["id"])
    branch = str(task["branch"])
    base_head = subprocess.run(
        ["git", "-C", repo, "rev-parse", "main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    preserved_commit: str | None = None
    worktree_path = paths.run_dir / "worktrees" / task_id / "repo"
    worker_calls = 0

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal preserved_commit, worker_calls
        worker_calls += 1
        worktree = Path(kwargs["worktree_repo_path"])
        if worker_calls == 1:
            (worktree / "progress.txt").write_text("committed progress\n", encoding="utf-8")
            subprocess.run(["git", "add", "progress.txt"], cwd=worktree, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test User",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "-m",
                    "progress",
                ],
                cwd=worktree,
                check=True,
            )
            preserved_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            (worktree / "stale.txt").write_text("leftover\n", encoding="utf-8")
            return _worker_result_for_path(
                run_paths=paths,
                task_id=task_id,
                branch=branch,
                worktree_path=worktree,
                success=False,
                summary="worker failed",
                commit_hash=preserved_commit,
                error="worker failed",
                changed_files=["progress.txt"],
            )

        assert preserved_commit is not None
        assert not (worktree / "stale.txt").exists()
        assert not (worktree / "progress.txt").exists()
        current_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert current_head == base_head
        return _worker_result_for_path(
            run_paths=paths,
            task_id=task_id,
            branch=branch,
            worktree_path=worktree,
            success=True,
            summary="ok",
            commit_hash=current_head,
            changed_files=["progress.txt"],
        )

    code_first = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=False,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        console=_null_console(),
        worker_runner=fake_worker_runner,
        merge_runner=lambda *_a, **_k: "merge-feat/t01-a",
    )
    assert code_first == 1
    assert not worktree_path.exists()
    assert (
        subprocess.run(
            ["git", "-C", repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            check=False,
        ).returncode
        == 1
    )

    plan_retry = _load_json(paths.plan_json_path)
    code_retry = run_swarm(
        paths=paths,
        plan=plan_retry,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=False,
        retry_failed=True,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        console=_null_console(),
        worker_runner=fake_worker_runner,
        merge_runner=lambda *_a, **_k: "merge-feat/t01-a",
    )
    assert code_retry == 0


def test_swarm_rejects_non_auto_mode_unless_dry_run(tmp_path: Path) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    reject = runner.invoke(
        alysis_app,
        [
            "forge",
            "swarm",
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--mode",
            "review",
        ],
        env=_env(tmp_path),
    )
    assert reject.exit_code == 2
    assert "swarm requires --mode auto (non-interactive)" in reject.output

    allow_dry_run = runner.invoke(
        alysis_app,
        [
            "forge",
            "swarm",
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--mode",
            "review",
            "--dry-run",
            "--base-branch",
            "main",
        ],
        env=_env(tmp_path),
    )
    assert allow_dry_run.exit_code == 0


def test_swarm_recovers_stale_in_progress_tasks_on_start(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_head(repo)
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Stale task", estimated_files=["src/a.py"], branch="feat/t01-a")
    task["status"] = "in_progress"
    task["attempts"] = 1
    save_plan(paths, plan)

    called = {"pruned": False}

    def fake_prune(_root: Path) -> None:
        called["pruned"] = True

    monkeypatch.setattr("alysis_code.swarm_orchestrator.prune_worktrees", fake_prune)

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=True,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="warn",
        verify_cmd=["pytest -q"],
        console=_null_console(),
        worker_runner=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("worker should not run in dry-run")
        ),
        merge_runner=lambda *_a, **_k: "merge",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 0
    assert called["pruned"] is True

    final_plan = _load_json(paths.plan_json_path)
    recovered_task = final_plan["tasks"][0]
    assert recovered_task["status"] == "failed"
    assert recovered_task["attempts"] == 1
    assert "Recovered stale in_progress task" in recovered_task["last_error"]

    summary = (paths.execution_dir / "swarm_summary.md").read_text(encoding="utf-8")
    assert "## Recovered" in summary
    assert task["id"] in summary


def test_swarm_recovered_task_rerun_bumps_attempt_once(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Recovered task", estimated_files=["src/a.py"], branch="feat/t01-a")
    task["status"] = "in_progress"
    task["attempts"] = 1
    save_plan(paths, plan)

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        t = kwargs["task"]
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(t["id"]),
            branch=str(t["branch"]),
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=True,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="warn",
        verify_cmd=["pytest -q"],
        integration_mode="off",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        integration_runner=lambda **kwargs: _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=list(kwargs["merged_task_ids"]),
            passed=True,
            summary="integration passed",
            phase=kwargs.get("phase", "post_merge"),
        ),
        merge_runner=lambda *_a, **_k: "merge",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 0

    final_plan = _load_json(paths.plan_json_path)
    final_task = next(entry for entry in final_plan["tasks"] if entry["id"] == task["id"])
    assert final_task["status"] == "done"
    assert final_task["attempts"] == 2


def test_swarm_cleanup_failure_after_merge_keeps_done_status(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_head(repo)
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    t1 = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        task = kwargs["task"]
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(task["id"]),
            branch=str(task["branch"]),
        )

    def fake_merge_runner(_root, *, base_branch: str, task_branch: str, message: str) -> str:
        assert task_branch == "feat/t01-a"
        assert message.startswith("Merge ")
        return "merge-feat/t01-a"

    def fail_delete(_root: Path, _branch: str) -> None:
        raise GitOpsError("cannot delete branch")

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=False,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        integration_mode="off",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        integration_runner=lambda **kwargs: _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=list(kwargs["merged_task_ids"]),
            passed=True,
            summary="integration passed",
            phase=str(kwargs.get("phase") or "post_merge"),
        ),
        merge_runner=fake_merge_runner,
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=fail_delete,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 0

    final_plan = _load_json(paths.plan_json_path)
    statuses = {task["id"]: task["status"] for task in final_plan["tasks"]}
    assert statuses[t1["id"]] == "done"

    merge_result = _load_json(paths.execution_dir / "merge_results" / f"{t1['id']}.json")
    assert merge_result["success"] is True
    assert "branch cleanup failed" in str(merge_result.get("cleanup_error"))


def test_swarm_review_rejection_sets_changes_requested_and_skips_merge(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    merges: list[str] = []

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        t = kwargs["task"]
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(t["id"]),
            branch=str(t["branch"]),
        )

    def fake_review_runner(*, task: dict, **_kwargs):  # type: ignore[no-untyped-def]
        task_id = str(task["id"])
        json_path = paths.execution_dir / "reviews" / f"{task_id}.json"
        md_path = paths.execution_dir / "reviews" / f"{task_id}.md"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text("{}", encoding="utf-8")
        md_path.write_text("# review\n", encoding="utf-8")
        return ReviewOutcome(
            task_id=task_id,
            approved=False,
            confidence="medium",
            summary="changes requested",
            blocking_issues_count=1,
            non_blocking_issues_count=0,
            json_path=json_path,
            markdown_path=md_path,
        )

    def fake_merge_runner(_root, *, base_branch: str, task_branch: str, message: str) -> str:
        merges.append(task_branch)
        return "merge-commit"

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=True,
        console=_null_console(),
        worker_runner=fake_worker_runner,
        review_runner=fake_review_runner,
        merge_runner=fake_merge_runner,
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 1
    assert merges == []

    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[task["id"]] == "changes_requested"


def test_swarm_verify_strict_failure_marks_verify_failed_and_skips_merge(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    merges: list[str] = []

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        t = kwargs["task"]
        return TaskWorkerResult(
            task_id=str(t["id"]),
            title=str(t["title"]),
            branch=str(t["branch"]),
            worktree_path=os.fspath(paths.run_dir / "worktrees" / str(t["id"]) / "repo"),
            started_at="2026-02-19T00:00:00+00:00",
            finished_at="2026-02-19T00:01:00+00:00",
            success=False,
            summary="strict verification failed",
            commit_hash="deadbeef",
            error="strict verification failed",
            report_path="r.md",
            patch_path="p.diff",
            log_path="l.jsonl",
            log_pointer_path="lp.json",
            warnings=[],
            changed_files=["src/a.py"],
            verify_failed=True,
            verify_summary="verification failed (0/1)",
            verify_artifact_path=".alysis/runs/x/execution/verify/T01.txt",
        )

    def fake_merge_runner(_root, *, base_branch: str, task_branch: str, message: str) -> str:
        merges.append(task_branch)
        return "merge-commit"

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="strict",
        verify_cmd=["pytest -q"],
        console=_null_console(),
        worker_runner=fake_worker_runner,
        merge_runner=fake_merge_runner,
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 1
    assert merges == []

    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[task["id"]] == "verify_failed"


def test_swarm_warn_mode_treats_pytest_no_tests_collected_as_clean_verification(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    merges: list[str] = []

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["verify_commands"] == ["pytest -q"]
        t = kwargs["task"]
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(t["id"]),
            branch=str(t["branch"]),
            changed_files=["src/a.py"],
            verify_summary="verification skipped: nothing to verify (1/1)",
            verify_payload={
                "commands": ["pytest -q"],
                "all_passed": True,
                "command_results": [
                    {
                        "command": "pytest -q",
                        "effective_command": "pytest -q",
                        "exit_code": 5,
                        "status": "skipped",
                        "ok": True,
                        "real_execution": False,
                        "non_execution_reason": "pytest_no_tests_collected",
                    }
                ],
            },
        )

    def fake_merge_runner(_root, *, base_branch: str, task_branch: str, message: str) -> str:
        merges.append(task_branch)
        return "merge-commit"

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="warn",
        verify_cmd=["pytest -q"],
        integration_mode="off",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        merge_runner=fake_merge_runner,
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )

    assert code == 0
    assert merges == ["feat/t01-a"]
    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[task["id"]] == "done"
    summary = (paths.execution_dir / "swarm_summary.md").read_text(encoding="utf-8")
    assert "verification did not execute tests: pytest_no_tests_collected" not in summary
    assert "- Run Status: `clean`" in summary
    assert "- Clean: `yes`" in summary
    assert "- Verification Status: `not_run`" in summary


def test_swarm_blocks_merge_that_would_overwrite_untracked_workspace_file(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_head(repo)
    (repo / "USER_NOTES.md").write_text("user-owned notes\n", encoding="utf-8")

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Update user notes",
        estimated_files=["USER_NOTES.md"],
        branch="feat/t01-user-notes",
    )
    save_plan(paths, plan)

    merges: list[str] = []

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        t = kwargs["task"]
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(t["id"]),
            branch=str(t["branch"]),
            changed_files=["USER_NOTES.md"],
        )

    def fake_merge_runner(_root, *, base_branch: str, task_branch: str, message: str) -> str:
        merges.append(task_branch)
        return "merge-commit"

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="warn",
        verify_cmd=["pytest -q"],
        integration_mode="off",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        merge_runner=fake_merge_runner,
    )

    assert code == 1
    assert merges == []
    assert (repo / "USER_NOTES.md").read_text(encoding="utf-8") == "user-owned notes\n"
    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[task["id"]] == "failed"
    summary = (paths.execution_dir / "swarm_summary.md").read_text(encoding="utf-8")
    assert "blocked to protect untracked workspace files" in summary


def test_swarm_runs_integration_gate_after_merged_batch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    gate_calls: list[tuple[int, tuple[str, ...]]] = []

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        t = kwargs["task"]
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(t["id"]),
            branch=str(t["branch"]),
        )

    def fake_integration_runner(**kwargs):  # type: ignore[no-untyped-def]
        gate_calls.append((kwargs["batch_index"], tuple(kwargs["merged_task_ids"])))
        return _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=list(kwargs["merged_task_ids"]),
            passed=True,
            summary="integration passed",
            phase=str(kwargs.get("phase") or "post_merge"),
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode="warn",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        integration_runner=fake_integration_runner,
        merge_runner=lambda *_a, **_k: "merge-commit",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 0
    assert gate_calls == [(1, (str(task["id"]),))]
    summary = (paths.execution_dir / "swarm_summary.md").read_text(encoding="utf-8")
    assert "## Integration Gates" in summary
    assert "pre-merge candidate passed (warn)" in summary


def test_swarm_skips_integration_gate_when_no_tasks_merge(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    gate_calls: list[int] = []

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        t = kwargs["task"]
        return TaskWorkerResult(
            task_id=str(t["id"]),
            title=str(t["title"]),
            branch=str(t["branch"]),
            worktree_path=os.fspath(paths.run_dir / "worktrees" / str(t["id"]) / "repo"),
            started_at="2026-02-19T00:00:00+00:00",
            finished_at="2026-02-19T00:01:00+00:00",
            success=False,
            summary="worker failed",
            commit_hash=None,
            error="worker failed",
            report_path="r.md",
            patch_path="p.diff",
            log_path="l.jsonl",
            log_pointer_path="lp.json",
            warnings=[],
            changed_files=["src/a.py"],
            verify_failed=False,
            verify_summary=None,
            verify_artifact_path=None,
        )

    def fake_integration_runner(**kwargs):  # type: ignore[no-untyped-def]
        gate_calls.append(int(kwargs["batch_index"]))
        return _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=list(kwargs["merged_task_ids"]),
            passed=True,
            summary="integration passed",
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode="warn",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        integration_runner=fake_integration_runner,
        merge_runner=lambda *_a, **_k: "merge-commit",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 1
    assert gate_calls == []


def test_swarm_integration_gate_warn_continues_later_batches(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    first = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    second = add_task(
        plan,
        title="Task B",
        estimated_files=["src/b.py"],
        branch="feat/t02-b",
    )
    save_plan(paths, plan)

    worker_calls: list[str] = []
    gate_calls: list[int] = []

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        t = kwargs["task"]
        worker_calls.append(str(t["id"]))
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(t["id"]),
            branch=str(t["branch"]),
        )

    def fake_integration_runner(**kwargs):  # type: ignore[no-untyped-def]
        gate_calls.append(int(kwargs["batch_index"]))
        passed = kwargs["batch_index"] != 1
        summary = "integration failed" if not passed else "integration passed"
        return _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=list(kwargs["merged_task_ids"]),
            passed=passed,
            summary=summary,
            phase=str(kwargs.get("phase") or "post_merge"),
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode="warn",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        integration_runner=fake_integration_runner,
        merge_runner=lambda *_a, **_k: "merge-commit",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 1
    assert worker_calls == [str(first["id"]), str(second["id"])]
    assert gate_calls == [1, 2]
    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[str(first["id"])] == "candidate_rejected"
    assert statuses[str(second["id"])] == "done"
    issue_files = list((paths.knowledge_issues_dir / "batch_001").glob("*.md"))
    assert issue_files
    issue_text = issue_files[0].read_text(encoding="utf-8")
    assert "integration verification failed" in issue_text


def test_swarm_integration_gate_strict_blocks_later_batches(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    first = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    second = add_task(
        plan,
        title="Task B",
        estimated_files=["src/b.py"],
        branch="feat/t02-b",
    )
    save_plan(paths, plan)

    worker_calls: list[str] = []
    gate_calls: list[int] = []

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        t = kwargs["task"]
        worker_calls.append(str(t["id"]))
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(t["id"]),
            branch=str(t["branch"]),
        )

    def fake_integration_runner(**kwargs):  # type: ignore[no-untyped-def]
        gate_calls.append(int(kwargs["batch_index"]))
        return _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=list(kwargs["merged_task_ids"]),
            passed=False,
            summary="integration failed",
            phase=str(kwargs.get("phase") or "post_merge"),
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode="strict",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        integration_runner=fake_integration_runner,
        merge_runner=lambda *_a, **_k: "merge-commit",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 1
    assert worker_calls == [str(first["id"])]
    assert gate_calls == [1]
    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[str(first["id"])] == "candidate_rejected"
    assert statuses[str(second["id"])] == "blocked_integration"


def test_swarm_skips_pre_merge_candidate_when_integration_mode_off(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_head(repo)
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    gate_calls: list[tuple[str, str | None]] = []
    merge_calls: list[str] = []

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        current_task = kwargs["task"]
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(current_task["id"]),
            branch=str(current_task["branch"]),
        )

    def fake_integration_runner(**kwargs):  # type: ignore[no-untyped-def]
        gate_calls.append((str(kwargs["mode"]), kwargs.get("phase")))
        return _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=list(kwargs["merged_task_ids"]),
            passed=True,
            summary="integration passed",
            phase=str(kwargs.get("phase") or "post_merge"),
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode="off",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        integration_runner=fake_integration_runner,
        merge_runner=lambda *_a, **kwargs: (
            merge_calls.append(str(kwargs["task_branch"])) or "merge-commit"
        ),
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )

    assert code == 0
    assert gate_calls == []
    assert merge_calls == [str(task["branch"])]
    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[str(task["id"])] == "done"
    summary = (paths.execution_dir / "swarm_summary.md").read_text(encoding="utf-8")
    assert "Integration verification is explicitly off" in summary


def test_swarm_defaults_integration_gate_to_warn_mode(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_head(repo)
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    gate_calls: list[tuple[str, str | None]] = []

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        current_task = kwargs["task"]
        worktree_repo_path = Path(kwargs["worktree_repo_path"])
        commit_hash = _commit_repo_update(
            worktree_repo_path,
            "src/a.py",
            "VALUE = 1\n",
        )
        return _worker_result_for_path(
            run_paths=paths,
            task_id=str(current_task["id"]),
            branch=str(current_task["branch"]),
            worktree_path=worktree_repo_path,
            success=True,
            summary="ok",
            commit_hash=commit_hash,
            changed_files=["src/a.py"],
            verify_summary="verification disabled (--verify off)",
        )

    def fake_integration_runner(**kwargs):  # type: ignore[no-untyped-def]
        gate_calls.append((str(kwargs["mode"]), kwargs.get("phase")))
        return _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=list(kwargs["merged_task_ids"]),
            passed=True,
            summary="integration passed",
            phase=str(kwargs.get("phase") or "post_merge"),
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch=None,
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode=None,
        console=_null_console(),
        worker_runner=fake_worker_runner,
        integration_runner=fake_integration_runner,
    )

    assert code == 0
    assert gate_calls == [("warn", "pre_merge_candidate")]


def test_swarm_warn_allows_transient_red_candidate_with_unfinished_dependent(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "workspace"
    repo.mkdir()
    _init_git_repo_with_head(repo)
    (repo / "producer.py").write_text(
        "def event():\n    return {'type': 'ping', 'data': {}}\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("Contract uses type/data.\n", encoding="utf-8")
    subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            repo,
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "contract fixture",
        ],
        check=True,
    )

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    producer = add_task(
        plan,
        title="Update producer contract",
        estimated_files=["producer.py"],
        write_scope=["producer.py"],
        branch="feat/t01-producer",
    )
    docs = add_task(
        plan,
        title="Update README contract docs",
        dependencies=[str(producer["id"])],
        estimated_files=["README.md"],
        write_scope=["README.md"],
        branch="feat/t02-docs",
    )
    save_plan(paths, plan)

    gate_batches: list[list[str]] = []

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        task = kwargs["task"]
        worktree_repo_path = Path(kwargs["worktree_repo_path"])
        if str(task["id"]) == str(producer["id"]):
            commit_hash = _commit_repo_update(
                worktree_repo_path,
                "producer.py",
                "def event():\n    return {'event_type': 'ping', 'payload': {}}\n",
            )
            changed_files = ["producer.py"]
        else:
            commit_hash = _commit_repo_update(
                worktree_repo_path,
                "README.md",
                "Contract uses event_type/payload.\n",
            )
            changed_files = ["README.md"]
        return _worker_result_for_path(
            run_paths=paths,
            task_id=str(task["id"]),
            branch=str(task["branch"]),
            worktree_path=worktree_repo_path,
            success=True,
            summary="ok",
            commit_hash=commit_hash,
            changed_files=changed_files,
            verify_summary="verification passed (1/1)",
        )

    def fake_integration_runner(**kwargs):  # type: ignore[no-untyped-def]
        merged_task_ids = list(kwargs["merged_task_ids"])
        gate_batches.append(merged_task_ids)
        passed = len(gate_batches) > 1
        return _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=merged_task_ids,
            passed=passed,
            summary="integration passed" if passed else "README contract still stale",
            phase=str(kwargs.get("phase") or "post_merge"),
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch=None,
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode="warn",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        integration_runner=fake_integration_runner,
    )

    assert code == 0
    assert gate_batches == [
        [str(producer["id"])],
        [str(docs["id"])],
        [str(producer["id"]), str(docs["id"])],
    ]
    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[str(producer["id"])] == "done"
    assert statuses[str(docs["id"])] == "done"
    summary_json = _load_json(paths.execution_dir / "swarm_summary.json")
    assert summary_json["status"] == "clean"
    assert summary_json["clean"] is True
    assert summary_json["integration"]["final_batch"] == "batch_003"
    issue_files = list((paths.knowledge_issues_dir / "batch_001").glob("*.md"))
    assert issue_files
    assert "event_type/payload" in (repo / "README.md").read_text(encoding="utf-8")


def test_swarm_warn_final_repo_failure_reports_non_clean_status(tmp_path: Path) -> None:
    repo = tmp_path / "workspace"
    repo.mkdir()
    _init_git_repo_with_head(repo)
    (repo / "producer.py").write_text(
        "def event():\n    return {'type': 'ping', 'data': {}}\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("Contract uses type/data.\n", encoding="utf-8")
    subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            repo,
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "contract fixture",
        ],
        check=True,
    )

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    producer = add_task(
        plan,
        title="Update producer contract",
        estimated_files=["producer.py"],
        write_scope=["producer.py"],
        branch="feat/t01-producer",
    )
    docs = add_task(
        plan,
        title="Update README contract docs",
        dependencies=[str(producer["id"])],
        estimated_files=["README.md"],
        write_scope=["README.md"],
        branch="feat/t02-docs",
    )
    save_plan(paths, plan)

    gate_phases: list[str] = []

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        task = kwargs["task"]
        worktree_repo_path = Path(kwargs["worktree_repo_path"])
        if str(task["id"]) == str(producer["id"]):
            commit_hash = _commit_repo_update(
                worktree_repo_path,
                "producer.py",
                "def event():\n    return {'event_type': 'ping', 'payload': {}}\n",
            )
            changed_files = ["producer.py"]
        else:
            commit_hash = _commit_repo_update(
                worktree_repo_path,
                "README.md",
                "Contract uses event_type/payload.\n",
            )
            changed_files = ["README.md"]
        return _worker_result_for_path(
            run_paths=paths,
            task_id=str(task["id"]),
            branch=str(task["branch"]),
            worktree_path=worktree_repo_path,
            success=True,
            summary="ok",
            commit_hash=commit_hash,
            changed_files=changed_files,
            verify_summary="verification passed (1/1)",
        )

    def fake_integration_runner(**kwargs):  # type: ignore[no-untyped-def]
        phase = str(kwargs.get("phase") or "post_merge")
        gate_phases.append(phase)
        passed = phase == "pre_merge_candidate" and len(gate_phases) > 1
        return _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=list(kwargs["merged_task_ids"]),
            passed=passed,
            summary="integration passed" if passed else "final repo validation failed",
            phase=phase,
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch=None,
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode="warn",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        integration_runner=fake_integration_runner,
    )

    assert code == 1
    assert gate_phases == ["pre_merge_candidate", "pre_merge_candidate", "final_repo"]
    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[str(producer["id"])] == "done"
    assert statuses[str(docs["id"])] == "done"
    summary_json = _load_json(paths.execution_dir / "swarm_summary.json")
    assert summary_json["status"] == "integration_failed_warn_mode"
    assert summary_json["clean"] is False
    assert summary_json["verification_status"] == "failed_tolerated_by_warn_policy"
    summary_md = (paths.execution_dir / "swarm_summary.md").read_text(encoding="utf-8")
    assert "- Run Status: `integration_failed_warn_mode`" in summary_md
    assert "final repo failed (warn)" in summary_md


def test_swarm_batches_expected_red_regression_with_dependent_implementation(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "workspace"
    repo.mkdir()
    (repo / "slugger.py").write_text(
        "def slug(value):\n    return value\n",
        encoding="utf-8",
    )

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    tests = add_task(
        plan,
        title="Add red regression tests for slug behavior",
        description="Tests capture the current bug and fail before fix.",
        acceptance_criteria=["The regression test fails before fix and passes after fix."],
        estimated_files=["tests/test_slugger.py"],
        write_scope=["tests/test_slugger.py"],
        branch="feat/t01-red-tests",
    )
    impl = add_task(
        plan,
        title="Fix slug behavior",
        description="Implement normalized slug output.",
        dependencies=[str(tests["id"])],
        estimated_files=["slugger.py"],
        write_scope=["slugger.py"],
        branch="feat/t02-impl",
    )
    save_plan(paths, plan)

    gate_batches: list[list[str]] = []

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        task = kwargs["task"]
        worktree_repo_path = Path(kwargs["worktree_repo_path"])
        if str(task["id"]) == str(tests["id"]):
            test_dir = worktree_repo_path / "tests"
            test_dir.mkdir(exist_ok=True)
            (test_dir / "test_slugger.py").write_text(
                "from slugger import slug\n\n\ndef test_slug_spaces():\n"
                "    assert slug('Hello World') == 'hello-world'\n",
                encoding="utf-8",
            )
            changed_files = ["tests/test_slugger.py"]
        else:
            (worktree_repo_path / "slugger.py").write_text(
                "def slug(value):\n    return value.strip().lower().replace(' ', '-')\n",
                encoding="utf-8",
            )
            changed_files = ["slugger.py"]
        subprocess.run(["git", "-C", worktree_repo_path, "add", "-A"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                worktree_repo_path,
                "-c",
                "user.name=Test User",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "task update",
            ],
            check=True,
        )
        return _snapshot_worker_result(run_paths=paths, task=task, changed_files=changed_files)

    def fake_integration_runner(**kwargs):  # type: ignore[no-untyped-def]
        merged_task_ids = list(kwargs["merged_task_ids"])
        gate_batches.append(merged_task_ids)
        return _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=merged_task_ids,
            passed=True,
            summary="combined candidate is green",
            phase=str(kwargs.get("phase") or "post_merge"),
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=2,
        base_branch=None,
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode="warn",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        integration_runner=fake_integration_runner,
    )

    assert code == 0
    assert gate_batches == [[str(tests["id"]), str(impl["id"])]]
    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[str(tests["id"])] == "done"
    assert statuses[str(impl["id"])] == "done"


def test_swarm_rejects_red_pre_merge_candidate_without_applying_snapshot_batch(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "workspace"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_calc.py").write_text("def test_add():\n    assert True\n", encoding="utf-8")

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    impl = add_task(plan, title="Implement calc", estimated_files=["calc.py"], branch="feat/t01-a")
    tests = add_task(
        plan,
        title="Add calc tests",
        estimated_files=["test_calc.py"],
        branch="feat/t02-b",
    )
    save_plan(paths, plan)

    merge_calls: list[tuple[str, str, str]] = []

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        task = kwargs["task"]
        worktree_repo_path = Path(kwargs["worktree_repo_path"])
        if str(task["id"]) == str(impl["id"]):
            (worktree_repo_path / "calc.py").write_text(
                "def add(a, b):\n    return a + b\n",
                encoding="utf-8",
            )
            changed_files = ["calc.py"]
        else:
            (worktree_repo_path / "test_calc.py").write_text(
                "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 4\n",
                encoding="utf-8",
            )
            changed_files = ["test_calc.py"]
        subprocess.run(["git", "-C", worktree_repo_path, "add", "-A"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                worktree_repo_path,
                "-c",
                "user.name=Test User",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "task update",
            ],
            check=True,
        )
        return _snapshot_worker_result(run_paths=paths, task=task, changed_files=changed_files)

    def fake_integration_runner(**kwargs):  # type: ignore[no-untyped-def]
        return _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=list(kwargs["merged_task_ids"]),
            passed=False,
            summary="combined candidate is red",
            phase=str(kwargs.get("phase") or "post_merge"),
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=2,
        base_branch=None,
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode="warn",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        integration_runner=fake_integration_runner,
        merge_runner=lambda repo_root, **kwargs: (
            merge_calls.append(
                (os.fspath(repo_root), str(kwargs["base_branch"]), str(kwargs["task_branch"]))
            )
            or "merge-commit"
        ),
    )

    assert code == 1
    assert merge_calls == []
    assert (repo / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a - b\n"
    assert (repo / "test_calc.py").read_text(encoding="utf-8") == (
        "def test_add():\n    assert True\n"
    )
    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[str(impl["id"])] == "candidate_rejected"
    assert statuses[str(tests["id"])] == "candidate_rejected"
    merge_result_impl = _load_json(paths.execution_dir / "merge_results" / f"{impl['id']}.json")
    merge_result_tests = _load_json(paths.execution_dir / "merge_results" / f"{tests['id']}.json")
    assert merge_result_impl["action"] == "candidate_rejected"
    assert merge_result_tests["action"] == "candidate_rejected"
    summary = (paths.execution_dir / "swarm_summary.md").read_text(encoding="utf-8")
    assert "rejected before merge/apply" in summary
    assert "pre-merge candidate failed (warn)" in summary


def test_swarm_accepts_green_pre_merge_candidate_and_applies_snapshot_batch(tmp_path: Path) -> None:
    repo = tmp_path / "workspace"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_calc.py").write_text("def test_add():\n    assert True\n", encoding="utf-8")

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    impl = add_task(plan, title="Implement calc", estimated_files=["calc.py"], branch="feat/t01-a")
    tests = add_task(
        plan,
        title="Add calc tests",
        estimated_files=["test_calc.py"],
        branch="feat/t02-b",
    )
    save_plan(paths, plan)

    merge_calls: list[tuple[str, str, str]] = []

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        task = kwargs["task"]
        worktree_repo_path = Path(kwargs["worktree_repo_path"])
        if str(task["id"]) == str(impl["id"]):
            (worktree_repo_path / "calc.py").write_text(
                "def add(a, b):\n    return a + b\n",
                encoding="utf-8",
            )
            changed_files = ["calc.py"]
        else:
            (worktree_repo_path / "test_calc.py").write_text(
                "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
                encoding="utf-8",
            )
            changed_files = ["test_calc.py"]
        subprocess.run(["git", "-C", worktree_repo_path, "add", "-A"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                worktree_repo_path,
                "-c",
                "user.name=Test User",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "task update",
            ],
            check=True,
        )
        return _snapshot_worker_result(run_paths=paths, task=task, changed_files=changed_files)

    def fake_integration_runner(**kwargs):  # type: ignore[no-untyped-def]
        return _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=list(kwargs["merged_task_ids"]),
            passed=True,
            summary="combined candidate is green",
            phase=str(kwargs.get("phase") or "post_merge"),
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=2,
        base_branch=None,
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode="warn",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        integration_runner=fake_integration_runner,
        merge_runner=lambda repo_root, **kwargs: (
            merge_calls.append(
                (os.fspath(repo_root), str(kwargs["base_branch"]), str(kwargs["task_branch"]))
            )
            or "merge-commit"
        ),
    )

    assert code == 0
    assert len(merge_calls) == 2
    assert (repo / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"
    assert (repo / "test_calc.py").read_text(encoding="utf-8") == (
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[str(impl["id"])] == "done"
    assert statuses[str(tests["id"])] == "done"
    summary = (paths.execution_dir / "swarm_summary.md").read_text(encoding="utf-8")
    assert "pre-merge candidate passed (warn)" in summary


def test_swarm_rejects_red_pre_merge_candidate_via_real_integration_gate_snapshot_backend(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "workspace"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_calc.py").write_text("def test_add():\n    assert True\n", encoding="utf-8")

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    impl = add_task(plan, title="Implement calc", estimated_files=["calc.py"], branch="feat/t01-a")
    tests = add_task(
        plan,
        title="Add calc tests",
        estimated_files=["test_calc.py"],
        branch="feat/t02-b",
    )
    save_plan(paths, plan)

    def worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        task = kwargs["task"]
        worktree_repo_path = Path(kwargs["worktree_repo_path"])
        task_id = str(task["id"])
        if task_id == str(impl["id"]):
            commit_hash = _commit_repo_update(
                worktree_repo_path,
                "calc.py",
                "def add(a, b):\n    return a + b\n",
            )
            changed_files = ["calc.py"]
        else:
            commit_hash = _commit_repo_update(
                worktree_repo_path,
                "test_calc.py",
                "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 4\n",
            )
            changed_files = ["test_calc.py"]
        return _worker_result_for_path(
            run_paths=paths,
            task_id=task_id,
            branch=str(task["branch"]),
            worktree_path=worktree_repo_path,
            success=True,
            summary="ok",
            commit_hash=commit_hash,
            changed_files=changed_files,
            verify_summary="verification disabled (--verify off)",
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=_host_verify_cfg(),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=2,
        base_branch=None,
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode=None,
        console=_null_console(),
        worker_runner=worker_runner,
    )

    assert code == 1
    assert (repo / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a - b\n"
    assert (repo / "test_calc.py").read_text(encoding="utf-8") == (
        "def test_add():\n    assert True\n"
    )
    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[str(impl["id"])] == "candidate_rejected"
    assert statuses[str(tests["id"])] == "candidate_rejected"
    result_payload = _load_json(paths.execution_integration_dir / "batch_001" / "result.json")
    assert result_payload["commands"] == ["pytest -q"]
    assert result_payload["command_source"] == "repo_scan.likely_test_commands_fallback"
    assert result_payload["passed"] is False
    assert result_payload["command_results"][0]["command"] == "pytest -q"
    assert result_payload["verified_root"].endswith("/_batch_candidates/batch_001/repo")
    merge_result_impl = _load_json(paths.execution_dir / "merge_results" / f"{impl['id']}.json")
    merge_result_tests = _load_json(paths.execution_dir / "merge_results" / f"{tests['id']}.json")
    assert merge_result_impl["action"] == "candidate_rejected"
    assert merge_result_tests["action"] == "candidate_rejected"


def test_swarm_rejects_red_pre_merge_candidate_via_real_integration_gate_git_backend(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_head(repo)
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_calc.py").write_text("def test_add():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            repo,
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "seed calc",
        ],
        check=True,
    )

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    impl = add_task(plan, title="Implement calc", estimated_files=["calc.py"], branch="feat/t01-a")
    tests = add_task(
        plan,
        title="Add calc tests",
        estimated_files=["test_calc.py"],
        branch="feat/t02-b",
    )
    save_plan(paths, plan)

    def worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        task = kwargs["task"]
        worktree_repo_path = Path(kwargs["worktree_repo_path"])
        task_id = str(task["id"])
        if task_id == str(impl["id"]):
            commit_hash = _commit_repo_update(
                worktree_repo_path,
                "calc.py",
                "def add(a, b):\n    return a + b\n",
            )
            changed_files = ["calc.py"]
        else:
            commit_hash = _commit_repo_update(
                worktree_repo_path,
                "test_calc.py",
                "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 4\n",
            )
            changed_files = ["test_calc.py"]
        return _worker_result_for_path(
            run_paths=paths,
            task_id=task_id,
            branch=str(task["branch"]),
            worktree_path=worktree_repo_path,
            success=True,
            summary="ok",
            commit_hash=commit_hash,
            changed_files=changed_files,
            verify_summary="verification disabled (--verify off)",
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=_host_verify_cfg(),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=2,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode=None,
        console=_null_console(),
        worker_runner=worker_runner,
    )

    assert code == 1
    assert (repo / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a - b\n"
    assert (repo / "test_calc.py").read_text(encoding="utf-8") == (
        "def test_add():\n    assert True\n"
    )
    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[str(impl["id"])] == "candidate_rejected"
    assert statuses[str(tests["id"])] == "candidate_rejected"
    result_payload = _load_json(paths.execution_integration_dir / "batch_001" / "result.json")
    assert result_payload["commands"] == ["pytest -q"]
    assert result_payload["command_source"] == "repo_scan.likely_test_commands_fallback"
    assert result_payload["passed"] is False
    assert result_payload["command_results"][0]["command"] == "pytest -q"
    assert result_payload["verified_root"].endswith("/_batch_candidates/batch_001/repo")


def test_swarm_accepts_green_pre_merge_candidate_via_real_integration_gate_git_backend(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_head(repo)
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_calc.py").write_text("def test_add():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            repo,
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "seed calc",
        ],
        check=True,
    )

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    impl = add_task(plan, title="Implement calc", estimated_files=["calc.py"], branch="feat/t01-a")
    tests = add_task(
        plan,
        title="Add calc tests",
        estimated_files=["test_calc.py"],
        branch="feat/t02-b",
    )
    save_plan(paths, plan)

    def worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        task = kwargs["task"]
        worktree_repo_path = Path(kwargs["worktree_repo_path"])
        task_id = str(task["id"])
        if task_id == str(impl["id"]):
            commit_hash = _commit_repo_update(
                worktree_repo_path,
                "calc.py",
                "def add(a, b):\n    return a + b\n",
            )
            changed_files = ["calc.py"]
        else:
            commit_hash = _commit_repo_update(
                worktree_repo_path,
                "test_calc.py",
                "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
            )
            changed_files = ["test_calc.py"]
        return _worker_result_for_path(
            run_paths=paths,
            task_id=task_id,
            branch=str(task["branch"]),
            worktree_path=worktree_repo_path,
            success=True,
            summary="ok",
            commit_hash=commit_hash,
            changed_files=changed_files,
            verify_summary="verification disabled (--verify off)",
        )

    integration_command = f"{sys.executable} -m pytest -q -s test_calc.py"
    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=_host_verify_cfg(),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=2,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode=None,
        integration_verify_cmd=[integration_command],
        console=_null_console(),
        worker_runner=worker_runner,
    )

    assert code == 0
    assert (repo / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"
    assert (repo / "test_calc.py").read_text(encoding="utf-8") == (
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[str(impl["id"])] == "done"
    assert statuses[str(tests["id"])] == "done"
    result_payload = _load_json(paths.execution_integration_dir / "batch_001" / "result.json")
    assert result_payload["commands"] == [integration_command]
    assert result_payload["command_source"] == "cli.integration_verify_cmd"
    assert result_payload["passed"] is True
    assert result_payload["command_results"][0]["command"] == integration_command
    assert result_payload["verified_root"].endswith("/_batch_candidates/batch_001/repo")


def test_swarm_accepts_green_pre_merge_candidate_via_real_integration_gate_snapshot_backend(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "workspace"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_calc.py").write_text("def test_add():\n    assert True\n", encoding="utf-8")

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    impl = add_task(plan, title="Implement calc", estimated_files=["calc.py"], branch="feat/t01-a")
    tests = add_task(
        plan,
        title="Add calc tests",
        estimated_files=["test_calc.py"],
        branch="feat/t02-b",
    )
    save_plan(paths, plan)

    def worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        task = kwargs["task"]
        worktree_repo_path = Path(kwargs["worktree_repo_path"])
        task_id = str(task["id"])
        if task_id == str(impl["id"]):
            commit_hash = _commit_repo_update(
                worktree_repo_path,
                "calc.py",
                "def add(a, b):\n    return a + b\n",
            )
            changed_files = ["calc.py"]
        else:
            commit_hash = _commit_repo_update(
                worktree_repo_path,
                "test_calc.py",
                "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
            )
            changed_files = ["test_calc.py"]
        return _worker_result_for_path(
            run_paths=paths,
            task_id=task_id,
            branch=str(task["branch"]),
            worktree_path=worktree_repo_path,
            success=True,
            summary="ok",
            commit_hash=commit_hash,
            changed_files=changed_files,
            verify_summary="verification disabled (--verify off)",
        )

    integration_command = f"{sys.executable} -m pytest -q -s test_calc.py"
    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=_host_verify_cfg(),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=2,
        base_branch=None,
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode=None,
        integration_verify_cmd=[integration_command],
        console=_null_console(),
        worker_runner=worker_runner,
    )

    assert code == 0
    assert (repo / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"
    assert (repo / "test_calc.py").read_text(encoding="utf-8") == (
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[str(impl["id"])] == "done"
    assert statuses[str(tests["id"])] == "done"
    result_payload = _load_json(paths.execution_integration_dir / "batch_001" / "result.json")
    assert result_payload["commands"] == [integration_command]
    assert result_payload["command_source"] == "cli.integration_verify_cmd"
    assert result_payload["passed"] is True


def test_swarm_skips_replanning_when_no_open_integration_issues(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    replan_calls: list[int] = []

    def fake_replanning_runner(**kwargs):  # type: ignore[no-untyped-def]
        replan_calls.append(int(kwargs["batch_index"]))
        return _replan_result(paths=paths, index=kwargs["batch_index"])

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model", replanning_mode="apply"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode="warn",
        console=_null_console(),
        worker_runner=lambda **kwargs: _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(kwargs["task"]["id"]),
            branch=str(kwargs["task"]["branch"]),
        ),
        integration_runner=lambda **kwargs: _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=list(kwargs["merged_task_ids"]),
            passed=True,
            summary="integration passed",
        ),
        replanning_runner=fake_replanning_runner,
        merge_runner=lambda *_a, **_k: "merge-commit",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 0
    assert replan_calls == []


def test_swarm_triggers_replanning_when_open_integration_issues_exist(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    replan_calls: list[int] = []

    def fake_replanning_runner(**kwargs):  # type: ignore[no-untyped-def]
        replan_calls.append(int(kwargs["batch_index"]))
        return _replan_result(paths=paths, index=kwargs["batch_index"])

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model", replanning_mode="suggest"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode="warn",
        console=_null_console(),
        worker_runner=lambda **kwargs: _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(kwargs["task"]["id"]),
            branch=str(kwargs["task"]["branch"]),
        ),
        integration_runner=lambda **kwargs: _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=list(kwargs["merged_task_ids"]),
            passed=False,
            summary="integration failed",
        ),
        replanning_runner=fake_replanning_runner,
        merge_runner=lambda *_a, **_k: "merge-commit",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 1
    assert replan_calls == [1]


def test_swarm_replanning_apply_updates_remaining_plan_only_after_validation(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    first = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    second = add_task(
        plan,
        title="Task B",
        estimated_files=["src/b.py"],
        dependencies=[str(first["id"])],
        branch="feat/t02-b",
    )
    save_plan(paths, plan)

    def fake_replanning_runner(**kwargs):  # type: ignore[no-untyped-def]
        return run_replanning_attempt(
            **kwargs,
            planner_runner=lambda **_planner_kwargs: PlannerTurnResult(
                assistant_message="Adjust remaining work.",
                questions=[],
                plan_update={
                    "tasks_update": [{"id": str(second["id"]), "title": "Task B (replanned)"}]
                },
            ),
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model", replanning_mode="apply"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode="warn",
        console=_null_console(),
        worker_runner=lambda **kwargs: _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(kwargs["task"]["id"]),
            branch=str(kwargs["task"]["branch"]),
        ),
        integration_runner=lambda **kwargs: _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=list(kwargs["merged_task_ids"]),
            passed=False if kwargs["batch_index"] == 1 else True,
            summary="integration failed" if kwargs["batch_index"] == 1 else "integration passed",
        ),
        replanning_runner=fake_replanning_runner,
        merge_runner=lambda *_a, **_k: "merge-commit",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 1
    final_plan = _load_json(paths.plan_json_path)
    updated_second = next(
        entry for entry in final_plan["tasks"] if entry["id"] == str(second["id"])
    )
    assert updated_second["title"] == "Task B (replanned)"
    assert (paths.plan_replans_dir / "replan_001" / "summary.md").exists()


def test_swarm_replanning_rejects_completed_task_rewrite(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    first = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    add_task(
        plan,
        title="Task B",
        estimated_files=["src/b.py"],
        dependencies=[str(first["id"])],
        branch="feat/t02-b",
    )
    save_plan(paths, plan)

    def fake_replanning_runner(**kwargs):  # type: ignore[no-untyped-def]
        return run_replanning_attempt(
            **kwargs,
            planner_runner=lambda **_planner_kwargs: PlannerTurnResult(
                assistant_message="Rewrite the completed task.",
                questions=[],
                plan_update={
                    "tasks_update": [{"id": str(first["id"]), "title": "Task A (bad replan)"}]
                },
            ),
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model", replanning_mode="apply"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode="warn",
        console=_null_console(),
        worker_runner=lambda **kwargs: _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(kwargs["task"]["id"]),
            branch=str(kwargs["task"]["branch"]),
        ),
        integration_runner=lambda **kwargs: _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=list(kwargs["merged_task_ids"]),
            passed=False if kwargs["batch_index"] == 1 else True,
            summary="integration failed" if kwargs["batch_index"] == 1 else "integration passed",
        ),
        replanning_runner=fake_replanning_runner,
        merge_runner=lambda *_a, **_k: "merge-commit",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 1
    final_plan = _load_json(paths.plan_json_path)
    first_task = next(entry for entry in final_plan["tasks"] if entry["id"] == str(first["id"]))
    assert first_task["title"] == "Task A"
    validation = _load_json(paths.plan_replans_dir / "replan_001" / "validation.json")
    assert validation["valid"] is False


def test_swarm_replanning_apply_bad_plan_fails_closed_before_next_worker(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    first = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    second = add_task(
        plan,
        title="Task B",
        estimated_files=["src/b.py"],
        dependencies=[str(first["id"])],
        branch="feat/t02-b",
    )
    save_plan(paths, plan)
    worker_calls: list[str] = []

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        task_id = str(kwargs["task"]["id"])
        worker_calls.append(task_id)
        if task_id == str(second["id"]):
            raise AssertionError("second worker should not run after bad applied replan")
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=task_id,
            branch=str(kwargs["task"]["branch"]),
        )

    def fake_replanning_runner(**kwargs):  # type: ignore[no-untyped-def]
        replanned_plan = kwargs["plan"]
        remaining = next(
            task for task in replanned_plan["tasks"] if task["id"] == str(second["id"])
        )
        remaining["estimated_files"] = [".alysis/something.json"]
        remaining["write_scope"] = [".alysis/something.json"]
        save_plan(paths, replanned_plan)
        return _replan_result(
            paths=paths,
            index=1,
            requested_mode="apply",
            effective_mode="apply",
            validation_passed=True,
            applied=True,
            plan_changed=True,
        )

    with pytest.raises(PlannerFailedError) as exc_info:
        run_swarm(
            paths=paths,
            plan=plan,
            cfg=AppConfig(model="test-model", replanning_mode="apply"),
            mode="auto",
            yes=False,
            max_steps=10,
            api_key_override="k",
            no_log=False,
            parallel=1,
            base_branch="main",
            max_tasks=None,
            max_attempts=None,
            dry_run=False,
            keep_worktrees=True,
            retry_failed=False,
            retry_changes_requested=False,
            only=None,
            retry_merge_conflicts=False,
            review=False,
            verify_mode="off",
            integration_mode="warn",
            console=_null_console(),
            worker_runner=fake_worker_runner,
            integration_runner=lambda **kwargs: _integration_result(
                paths=paths,
                batch_index=kwargs["batch_index"],
                mode=kwargs["mode"],
                merged_task_ids=list(kwargs["merged_task_ids"]),
                passed=False,
                summary="integration failed",
            ),
            replanning_runner=fake_replanning_runner,
            merge_runner=lambda *_a, **_k: "merge-commit",
            ensure_worktree_fn=lambda **_kwargs: None,
            remove_worktree_fn=lambda **_kwargs: None,
            delete_branch_fn=lambda _root, _branch: None,
            branch_exists_fn=lambda _root, _branch: True,
            current_branch_fn=lambda _root: "main",
        )

    assert exc_info.value.failure_category == FailureCategory.PLANNER_FAILED
    assert "R2" in str(exc_info.value)
    assert ".alysis/" in str(exc_info.value)
    assert worker_calls == [str(first["id"])]


def test_swarm_strict_integration_halts_even_with_replanning_apply(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    first = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    second = add_task(
        plan,
        title="Task B",
        estimated_files=["src/b.py"],
        dependencies=[str(first["id"])],
        branch="feat/t02-b",
    )
    save_plan(paths, plan)

    worker_calls: list[str] = []

    def fake_replanning_runner(**kwargs):  # type: ignore[no-untyped-def]
        return _replan_result(
            paths=paths,
            index=kwargs["batch_index"],
            requested_mode="apply",
            effective_mode="suggest",
            proposal_generated=True,
            validation_passed=True,
            applied=False,
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model", replanning_mode="apply"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode="strict",
        console=_null_console(),
        worker_runner=lambda **kwargs: (
            worker_calls.append(str(kwargs["task"]["id"]))
            or _ok_worker_result(
                run_dir=paths.run_dir,
                task_id=str(kwargs["task"]["id"]),
                branch=str(kwargs["task"]["branch"]),
            )
        ),
        integration_runner=lambda **kwargs: _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=list(kwargs["merged_task_ids"]),
            passed=False,
            summary="integration failed",
        ),
        replanning_runner=fake_replanning_runner,
        merge_runner=lambda *_a, **_k: "merge-commit",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 1
    assert worker_calls == [str(first["id"])]
    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[str(second["id"])] == "blocked_integration"
    assert final_plan["tasks"][1]["last_error"].startswith("blocked by strict integration gate")


def test_swarm_replanning_apply_recomputes_before_stale_runnable_task_runs(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    first = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    second = add_task(
        plan,
        title="Task B",
        estimated_files=["src/b.py"],
        dependencies=[str(first["id"])],
        branch="feat/t02-b",
    )
    third = add_task(
        plan,
        title="Task C",
        estimated_files=["src/c.py"],
        dependencies=[str(first["id"])],
        branch="feat/t03-c",
    )
    save_plan(paths, plan)

    worker_calls: list[str] = []
    third_seen_second_status: list[str] = []

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        task = kwargs["task"]
        plan_state = kwargs["plan"]
        task_id = str(task["id"])
        worker_calls.append(task_id)
        if task_id == str(third["id"]):
            second_entry = next(
                item for item in plan_state["tasks"] if item["id"] == str(second["id"])
            )
            third_seen_second_status.append(str(second_entry["status"]))
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=task_id,
            branch=str(task["branch"]),
        )

    def fake_replanning_runner(**kwargs):  # type: ignore[no-untyped-def]
        return run_replanning_attempt(
            **kwargs,
            planner_runner=lambda **_planner_kwargs: PlannerTurnResult(
                assistant_message="Make Task C depend on Task B before continuing.",
                questions=[],
                plan_update={
                    "tasks_update": [
                        {"id": str(third["id"]), "dependencies": [str(second["id"])]},
                    ]
                },
            ),
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model", replanning_mode="apply"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=2,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode="warn",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        integration_runner=lambda **kwargs: _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=list(kwargs["merged_task_ids"]),
            passed=False if kwargs["batch_index"] == 1 else True,
            summary="integration failed" if kwargs["batch_index"] == 1 else "integration passed",
        ),
        replanning_runner=fake_replanning_runner,
        merge_runner=lambda *_a, **_k: "merge-commit",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 1
    assert worker_calls == [str(first["id"])]
    assert third_seen_second_status == []
    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[str(first["id"])] == "candidate_rejected"
    assert statuses[str(second["id"])] == "planned"
    assert statuses[str(third["id"])] == "planned"
    assert final_plan["tasks"][2]["dependencies"] == [str(second["id"])]
    summary = (paths.execution_dir / "swarm_summary.md").read_text(encoding="utf-8")
    assert "recomputed=yes" in summary


def test_swarm_replanning_apply_removed_future_task_is_not_executed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    first = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    second = add_task(
        plan,
        title="Task B",
        estimated_files=["src/b.py"],
        dependencies=[str(first["id"])],
        branch="feat/t02-b",
    )
    third = add_task(
        plan,
        title="Task C",
        estimated_files=["src/c.py"],
        dependencies=[str(first["id"])],
        branch="feat/t03-c",
    )
    save_plan(paths, plan)

    worker_calls: list[str] = []

    def fake_replanning_runner(**kwargs):  # type: ignore[no-untyped-def]
        return run_replanning_attempt(
            **kwargs,
            planner_runner=lambda **_planner_kwargs: PlannerTurnResult(
                assistant_message="Remove Task C from the remaining plan.",
                questions=[],
                plan_update={"tasks_remove": [str(third["id"])]},
            ),
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model", replanning_mode="apply"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=2,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode="warn",
        console=_null_console(),
        worker_runner=lambda **kwargs: (
            worker_calls.append(str(kwargs["task"]["id"]))
            or _ok_worker_result(
                run_dir=paths.run_dir,
                task_id=str(kwargs["task"]["id"]),
                branch=str(kwargs["task"]["branch"]),
            )
        ),
        integration_runner=lambda **kwargs: _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=list(kwargs["merged_task_ids"]),
            passed=False if kwargs["batch_index"] == 1 else True,
            summary="integration failed" if kwargs["batch_index"] == 1 else "integration passed",
        ),
        replanning_runner=fake_replanning_runner,
        merge_runner=lambda *_a, **_k: "merge-commit",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 1
    assert worker_calls == [str(first["id"])]
    final_plan = _load_json(paths.plan_json_path)
    assert {entry["id"] for entry in final_plan["tasks"]} == {str(first["id"]), str(second["id"])}
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[str(first["id"])] == "candidate_rejected"
    assert statuses[str(second["id"])] == "planned"


def test_swarm_replanning_suggest_keeps_existing_schedule(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    first = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    second = add_task(
        plan,
        title="Task B",
        estimated_files=["src/b.py"],
        dependencies=[str(first["id"])],
        branch="feat/t02-b",
    )
    third = add_task(
        plan,
        title="Task C",
        estimated_files=["src/c.py"],
        dependencies=[str(first["id"])],
        branch="feat/t03-c",
    )
    save_plan(paths, plan)

    worker_calls: list[str] = []

    def fake_replanning_runner(**kwargs):  # type: ignore[no-untyped-def]
        return run_replanning_attempt(
            **kwargs,
            planner_runner=lambda **_planner_kwargs: PlannerTurnResult(
                assistant_message="Suggest removing Task C.",
                questions=[],
                plan_update={"tasks_remove": [str(third["id"])]},
            ),
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model", replanning_mode="suggest"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=2,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode="warn",
        console=_null_console(),
        worker_runner=lambda **kwargs: (
            worker_calls.append(str(kwargs["task"]["id"]))
            or _ok_worker_result(
                run_dir=paths.run_dir,
                task_id=str(kwargs["task"]["id"]),
                branch=str(kwargs["task"]["branch"]),
            )
        ),
        integration_runner=lambda **kwargs: _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=list(kwargs["merged_task_ids"]),
            passed=False if kwargs["batch_index"] == 1 else True,
            summary="integration failed" if kwargs["batch_index"] == 1 else "integration passed",
        ),
        replanning_runner=fake_replanning_runner,
        merge_runner=lambda *_a, **_k: "merge-commit",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 1
    assert worker_calls == [str(first["id"])]
    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[str(first["id"])] == "candidate_rejected"
    assert statuses[str(second["id"])] == "planned"
    assert statuses[str(third["id"])] == "planned"
    assert {entry["id"] for entry in final_plan["tasks"]} == {
        str(first["id"]),
        str(second["id"]),
        str(third["id"]),
    }
    summary = (paths.execution_dir / "swarm_summary.md").read_text(encoding="utf-8")
    assert "recomputed=yes" not in summary


def test_swarm_replanning_schedule_reset_preserves_max_tasks_accounting(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    first = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    second = add_task(
        plan,
        title="Task B",
        estimated_files=["src/b.py"],
        dependencies=[str(first["id"])],
        branch="feat/t02-b",
    )
    third = add_task(
        plan,
        title="Task C",
        estimated_files=["src/c.py"],
        dependencies=[str(first["id"])],
        branch="feat/t03-c",
    )
    save_plan(paths, plan)

    worker_calls: list[str] = []

    def fake_replanning_runner(**kwargs):  # type: ignore[no-untyped-def]
        return run_replanning_attempt(
            **kwargs,
            planner_runner=lambda **_planner_kwargs: PlannerTurnResult(
                assistant_message="Make Task C depend on Task B before continuing.",
                questions=[],
                plan_update={
                    "tasks_update": [
                        {"id": str(third["id"]), "dependencies": [str(second["id"])]},
                    ]
                },
            ),
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model", replanning_mode="apply"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=2,
        base_branch="main",
        max_tasks=2,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode="warn",
        console=_null_console(),
        worker_runner=lambda **kwargs: (
            worker_calls.append(str(kwargs["task"]["id"]))
            or _ok_worker_result(
                run_dir=paths.run_dir,
                task_id=str(kwargs["task"]["id"]),
                branch=str(kwargs["task"]["branch"]),
            )
        ),
        integration_runner=lambda **kwargs: _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=list(kwargs["merged_task_ids"]),
            passed=False if kwargs["batch_index"] == 1 else True,
            summary="integration failed" if kwargs["batch_index"] == 1 else "integration passed",
        ),
        replanning_runner=fake_replanning_runner,
        merge_runner=lambda *_a, **_k: "merge-commit",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 1
    assert worker_calls == [str(first["id"])]
    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[str(first["id"])] == "candidate_rejected"
    assert statuses[str(second["id"])] == "planned"
    assert statuses[str(third["id"])] == "planned"
    assert final_plan["tasks"][2]["dependencies"] == [str(second["id"])]


def test_swarm_max_attempts_skips_retry_failed_tasks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    task["status"] = "failed"
    task["attempts"] = 3
    save_plan(paths, plan)

    worker_calls: list[str] = []

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        worker_calls.append(str(kwargs["task"]["id"]))
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(kwargs["task"]["id"]),
            branch=str(kwargs["task"]["branch"]),
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=True,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="warn",
        verify_cmd=["pytest -q"],
        console=_null_console(),
        worker_runner=fake_worker_runner,
        merge_runner=lambda *_a, **_k: "merge",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 1
    assert worker_calls == []


def test_swarm_git_repo_without_head_uses_snapshot_backend(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-b", "main"], cwd=repo, check=True)
    (repo / "src.txt").write_text("before\n", encoding="utf-8")

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src.txt"], branch="feat/t01-a")
    save_plan(paths, plan)

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        worktree_repo_path = Path(kwargs["worktree_repo_path"])
        (worktree_repo_path / "src.txt").write_text("after\n", encoding="utf-8")
        subprocess.run(["git", "-C", worktree_repo_path, "add", "-A"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                worktree_repo_path,
                "-c",
                "user.name=Test User",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "task update",
            ],
            check=True,
        )
        return _snapshot_worker_result(run_paths=paths, task=task, changed_files=["src.txt"])

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode="off",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 0
    assert (repo / "src.txt").read_text(encoding="utf-8") == "after\n"
    summary = (paths.execution_dir / "swarm_summary.md").read_text(encoding="utf-8")
    assert "- Backend: `snapshot_workspace`" in summary


def test_swarm_real_git_repo_with_head_uses_git_worktree_backend(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_head(repo)

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=True,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="warn",
        verify_cmd=["pytest -q"],
        console=_null_console(),
        worker_runner=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("worker should not run in dry-run")
        ),
    )
    assert code == 0
    summary = (paths.execution_dir / "swarm_summary.md").read_text(encoding="utf-8")
    assert "- Backend: `git_worktree`" in summary


def test_swarm_plain_directory_uses_snapshot_backend_and_applies_changes(tmp_path: Path) -> None:
    repo = tmp_path / "workspace"
    repo.mkdir()
    (repo / "src.txt").write_text("before\n", encoding="utf-8")

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src.txt"], branch="feat/t01-a")
    save_plan(paths, plan)

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        worktree_repo_path = Path(kwargs["worktree_repo_path"])
        (worktree_repo_path / "src.txt").write_text("after\n", encoding="utf-8")
        subprocess.run(["git", "-C", worktree_repo_path, "add", "-A"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                worktree_repo_path,
                "-c",
                "user.name=Test User",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "task update",
            ],
            check=True,
        )
        return _snapshot_worker_result(run_paths=paths, task=task, changed_files=["src.txt"])

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch=None,
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode="off",
        console=_null_console(),
        worker_runner=fake_worker_runner,
    )
    assert code == 0
    assert (repo / "src.txt").read_text(encoding="utf-8") == "after\n"
    merge_result = _load_json(paths.execution_dir / "merge_results" / f"{task['id']}.json")
    assert merge_result["backend_name"] == "snapshot_workspace"
    assert merge_result["action"] == "applied"


def test_swarm_plain_directory_snapshot_does_not_apply_back_runtime_artifacts(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "workspace"
    repo.mkdir()
    (repo / "src.txt").write_text("before\n", encoding="utf-8")

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src.txt"], branch="feat/t01-a")
    save_plan(paths, plan)

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        worktree_repo_path = Path(kwargs["worktree_repo_path"])
        (worktree_repo_path / "src.txt").write_text("after\n", encoding="utf-8")
        (worktree_repo_path / "cli.pyc").write_bytes(b"legacy-pyc")
        (worktree_repo_path / "pkg" / "__pycache__").mkdir(parents=True)
        (worktree_repo_path / "pkg" / "__pycache__" / "mod.cpython-310.pyc").write_bytes(b"cache")
        subprocess.run(["git", "-C", worktree_repo_path, "add", "src.txt"], check=True)
        subprocess.run(["git", "-C", worktree_repo_path, "add", "-f", "cli.pyc"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                worktree_repo_path,
                "add",
                "-f",
                "pkg/__pycache__/mod.cpython-310.pyc",
            ],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                worktree_repo_path,
                "-c",
                "user.name=Test User",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "task update",
            ],
            check=True,
        )
        changed_files = subprocess.run(
            [
                "git",
                "-C",
                worktree_repo_path,
                "diff",
                "--name-only",
                "snapshot-base..HEAD",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        return _snapshot_worker_result(run_paths=paths, task=task, changed_files=changed_files)

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch=None,
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode="off",
        console=_null_console(),
        worker_runner=fake_worker_runner,
    )
    assert code == 0
    assert (repo / "src.txt").read_text(encoding="utf-8") == "after\n"
    assert not (repo / "cli.pyc").exists()
    assert not (repo / "pkg" / "__pycache__").exists()


def test_swarm_snapshot_backend_propagates_deleted_files(tmp_path: Path) -> None:
    repo = tmp_path / "workspace"
    repo.mkdir()
    (repo / "delete_me.txt").write_text("bye\n", encoding="utf-8")

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Task A",
        estimated_files=["delete_me.txt"],
        branch="feat/t01-a",
    )
    save_plan(paths, plan)

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        worktree_repo_path = Path(kwargs["worktree_repo_path"])
        (worktree_repo_path / "delete_me.txt").unlink()
        subprocess.run(["git", "-C", worktree_repo_path, "add", "-A"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                worktree_repo_path,
                "-c",
                "user.name=Test User",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "delete file",
            ],
            check=True,
        )
        return _snapshot_worker_result(
            run_paths=paths,
            task=task,
            changed_files=["delete_me.txt"],
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch=None,
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode="off",
        console=_null_console(),
        worker_runner=fake_worker_runner,
    )
    assert code == 0
    assert not (repo / "delete_me.txt").exists()


def test_swarm_plain_directory_sequential_tasks_do_not_leak_runtime_artifacts(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "workspace"
    repo.mkdir()
    (repo / "first.txt").write_text("before\n", encoding="utf-8")

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    first_task = add_task(plan, title="Task A", estimated_files=["first.txt"], branch="feat/t01-a")
    save_plan(paths, plan)

    observed: dict[str, bool] = {}

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        task = kwargs["task"]
        worktree_repo_path = Path(kwargs["worktree_repo_path"])
        if str(task["id"]) == str(first_task["id"]):
            (worktree_repo_path / "first.txt").write_text("after one\n", encoding="utf-8")
            (worktree_repo_path / "cli.pyc").write_bytes(b"legacy-pyc")
            (worktree_repo_path / "pkg" / "__pycache__").mkdir(parents=True)
            (worktree_repo_path / "pkg" / "__pycache__" / "mod.cpython-310.pyc").write_bytes(
                b"cache"
            )
            subprocess.run(["git", "-C", worktree_repo_path, "add", "-A"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    worktree_repo_path,
                    "-c",
                    "user.name=Test User",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "-m",
                    "task one",
                ],
                check=True,
            )
            return _snapshot_worker_result(
                run_paths=paths,
                task=task,
                changed_files=["first.txt"],
            )

        observed["top_level_pyc_in_second_snapshot"] = (worktree_repo_path / "cli.pyc").exists()
        observed["nested_pyc_in_second_snapshot"] = (
            worktree_repo_path / "pkg" / "__pycache__" / "mod.cpython-310.pyc"
        ).exists()
        (worktree_repo_path / "second.txt").write_text("after two\n", encoding="utf-8")
        subprocess.run(["git", "-C", worktree_repo_path, "add", "-A"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                worktree_repo_path,
                "-c",
                "user.name=Test User",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "task two",
            ],
            check=True,
        )
        return _snapshot_worker_result(
            run_paths=paths,
            task=task,
            changed_files=["second.txt"],
        )

    code_first = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch=None,
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode="off",
        console=_null_console(),
        worker_runner=fake_worker_runner,
    )
    assert code_first == 0
    assert (repo / "first.txt").read_text(encoding="utf-8") == "after one\n"
    assert not (repo / "cli.pyc").exists()
    assert not (repo / "pkg" / "__pycache__").exists()

    plan = load_plan(paths)
    add_task(plan, title="Task B", estimated_files=["second.txt"], branch="feat/t02-b")
    save_plan(paths, plan)

    code_second = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch=None,
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="off",
        integration_mode="off",
        console=_null_console(),
        worker_runner=fake_worker_runner,
    )
    assert code_second == 0
    assert observed == {
        "top_level_pyc_in_second_snapshot": False,
        "nested_pyc_in_second_snapshot": False,
    }
    assert (repo / "second.txt").read_text(encoding="utf-8") == "after two\n"
    assert not (repo / "cli.pyc").exists()
    assert not (repo / "pkg" / "__pycache__").exists()


def test_swarm_snapshot_backend_skips_remote_sync_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "workspace"
    repo.mkdir()

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.load_remote_settings_from_env",
        lambda: RemoteSettings(
            sync_mode="warn",
            remote_name="origin",
            create_pr=True,
            provider="auto",
        ),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.push_branch",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("push_branch should not run")),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.push_base",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("push_base should not run")),
    )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch=None,
        max_tasks=None,
        max_attempts=None,
        dry_run=True,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="warn",
        verify_cmd=["pytest -q"],
        console=_null_console(),
        worker_runner=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("worker should not run in dry-run")
        ),
    )
    assert code == 0
    summary = (paths.execution_dir / "swarm_summary.md").read_text(encoding="utf-8")
    assert (
        "Remote sync skipped: backend snapshot_workspace does not support branch push/PR flow."
        in summary
    )


def test_swarm_remote_sync_off_by_default_does_not_push(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.push_branch",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("push_branch should not run")),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.push_base",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("push_base should not run")),
    )

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        t = kwargs["task"]
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(t["id"]),
            branch=str(t["branch"]),
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="warn",
        verify_cmd=["pytest -q"],
        integration_mode="off",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        integration_runner=lambda **kwargs: _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=list(kwargs["merged_task_ids"]),
            passed=True,
            summary="integration passed",
            phase=str(kwargs.get("phase") or "post_merge"),
        ),
        merge_runner=lambda *_a, **_k: "merge-commit",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 0
    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[task["id"]] == "done"


def test_swarm_remote_warn_push_failure_allows_local_merge(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_head(repo)
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.load_remote_settings_from_env",
        lambda: RemoteSettings(
            sync_mode="warn",
            remote_name="origin",
            create_pr=False,
            provider="auto",
        ),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.get_remote_url",
        lambda *_a, **_k: "git@github.com:org/repo.git",
    )

    def fake_push_branch(_root: Path, *, remote: str, branch: str) -> tuple[bool, str]:
        calls.append((remote, branch))
        return False, "push failed"

    monkeypatch.setattr("alysis_code.swarm_orchestrator.push_branch", fake_push_branch)
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.push_base",
        lambda *_a, **_k: (False, "base push failed"),
    )

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        t = kwargs["task"]
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(t["id"]),
            branch=str(t["branch"]),
        )

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="warn",
        verify_cmd=["pytest -q"],
        integration_mode="off",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        integration_runner=lambda **kwargs: _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=list(kwargs["merged_task_ids"]),
            passed=True,
            summary="integration passed",
            phase=str(kwargs.get("phase") or "post_merge"),
        ),
        merge_runner=lambda *_a, **_k: "merge-commit",
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 0
    assert calls == [("origin", "feat/t01-a")]
    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[task["id"]] == "done"
    remote_json = _load_json(paths.execution_dir / "remote" / f"{task['id']}.json")
    assert remote_json["pushed_branch"] is False


def test_swarm_remote_strict_push_failure_blocks_merge(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_head(repo)
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)
    merges: list[str] = []

    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.load_remote_settings_from_env",
        lambda: RemoteSettings(
            sync_mode="strict",
            remote_name="origin",
            create_pr=False,
            provider="auto",
        ),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.get_remote_url",
        lambda *_a, **_k: "git@github.com:org/repo.git",
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.push_branch",
        lambda *_a, **_k: (False, "push failed"),
    )

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        t = kwargs["task"]
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(t["id"]),
            branch=str(t["branch"]),
        )

    def fake_merge_runner(_root, *, base_branch: str, task_branch: str, message: str) -> str:
        if base_branch == "main":
            merges.append(task_branch)
        return "merge-commit"

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="warn",
        verify_cmd=["pytest -q"],
        integration_mode="off",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        integration_runner=lambda **kwargs: _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=list(kwargs["merged_task_ids"]),
            passed=True,
            summary="integration passed",
            phase=str(kwargs.get("phase") or "post_merge"),
        ),
        merge_runner=fake_merge_runner,
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 1
    assert merges == []
    final_plan = _load_json(paths.plan_json_path)
    statuses = {entry["id"]: entry["status"] for entry in final_plan["tasks"]}
    assert statuses[task["id"]] == "failed"
    remote_json = _load_json(paths.execution_dir / "remote" / f"{task['id']}.json")
    assert remote_json["pushed_branch"] is False


def test_swarm_remote_strict_existing_pr_does_not_block_merge(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_head(repo)
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)
    merges: list[str] = []

    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.load_remote_settings_from_env",
        lambda: RemoteSettings(
            sync_mode="strict",
            remote_name="origin",
            create_pr=True,
            provider="auto",
        ),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.get_remote_url",
        lambda *_a, **_k: "git@github.com:org/repo.git",
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.push_branch",
        lambda *_a, **_k: (True, "pushed"),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.ensure_pr_or_mr",
        lambda *_a, **_k: (
            True,
            "https://github.com/org/repo/pull/77",
            "77",
            "existing",
        ),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_orchestrator.push_base",
        lambda *_a, **_k: (True, "base pushed"),
    )

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        t = kwargs["task"]
        return _ok_worker_result(
            run_dir=paths.run_dir,
            task_id=str(t["id"]),
            branch=str(t["branch"]),
        )

    def fake_merge_runner(_root, *, base_branch: str, task_branch: str, message: str) -> str:
        if base_branch == "main":
            merges.append(task_branch)
        return "merge-commit"

    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=True,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        verify_mode="warn",
        verify_cmd=["pytest -q"],
        integration_mode="off",
        console=_null_console(),
        worker_runner=fake_worker_runner,
        integration_runner=lambda **kwargs: _integration_result(
            paths=paths,
            batch_index=kwargs["batch_index"],
            mode=kwargs["mode"],
            merged_task_ids=list(kwargs["merged_task_ids"]),
            passed=True,
            summary="integration passed",
            phase=str(kwargs.get("phase") or "post_merge"),
        ),
        merge_runner=fake_merge_runner,
        ensure_worktree_fn=lambda **_kwargs: None,
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda _root, _branch: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
    )
    assert code == 0
    assert merges == ["feat/t01-a"]
    final_plan = _load_json(paths.plan_json_path)
    assert final_plan["tasks"][0]["status"] == "done"
    assert final_plan["tasks"][0]["remote_pr_url"] == "https://github.com/org/repo/pull/77"
    assert final_plan["tasks"][0]["remote_provider"] == "github"
