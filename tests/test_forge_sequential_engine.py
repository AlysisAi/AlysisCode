from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import traceback
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import alysis_code.forge_sequential_engine as engine_mod
from alysis_code import cli as cli_mod
from alysis_code.cli_impl import forge as forge_impl
from alysis_code.config import AppConfig
from alysis_code.execution_deadline import ExecutionDeadline
from alysis_code.forge import (
    RunPaths,
    add_task,
    create_plan_run,
    current_run_status,
    ensure_execution_dirs,
    load_plan,
    make_run_paths,
    save_plan,
    set_current_run_status,
)
from alysis_code.forge_events import ForgeEventEmitter
from alysis_code.forge_host_verification import (
    HostVerificationBinding,
    HostVerificationResult,
    RetainedArtifact,
)
from alysis_code.forge_sequential_engine import (
    BranchFacts,
    ExecutionTiming,
    ExecutionUsage,
    IntegrationCommitProof,
    Phase0ExecutionControls,
    PreparedIntegration,
    PreparedIntegrationFacts,
    SequentialRunDependencies,
    SequentialRunOutcome,
    SequentialRunProgress,
    SequentialRunRequest,
    SequentialTaskExecutionError,
    SequentialTaskRequest,
    TaskExecutionOutcome,
    run_sequential_forge,
    usage_from_session_logs,
)
from alysis_code.profiles import ProfileSpec, add_profile, set_active_profile
from alysis_code.run_state import (
    RUN_STATUS_APPROVED,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_INTERRUPTED,
    RUN_STATUS_RUNNING,
)


class _Guard:
    def __init__(self, *, acquired_after_wait: bool = False) -> None:
        self.acquired_after_wait = acquired_after_wait
        self.released = False

    def release(self) -> None:
        self.released = True


class _Clock:
    def __init__(self, value: float = 10.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _paths(tmp_path: Path) -> RunPaths:
    root = tmp_path / "repo"
    root.mkdir()
    paths = make_run_paths(root=root, run_id="native-run-1")
    paths.plan_dir.mkdir(parents=True, exist_ok=True)
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    return paths


def _plan() -> dict:
    return {
        "tasks": [
            {
                "id": "task-1",
                "title": "First",
                "status": "planned",
                "attempts": 0,
                "dependencies": [],
                "estimated_files": ["one.py"],
                "write_scope": ["one.py"],
            },
            {
                "id": "task-2",
                "title": "Second",
                "status": "planned",
                "attempts": 0,
                "dependencies": ["task-1"],
                "estimated_files": ["two.py"],
                "write_scope": ["two.py"],
            },
        ]
    }


def _outcome(
    task_request: SequentialTaskRequest,
    *,
    success: bool = True,
    usage: ExecutionUsage | None = None,
    timed_out: bool = False,
) -> TaskExecutionOutcome:
    paths = task_request.native_paths
    report = paths.execution_reports_dir / f"{task_request.task_id}.md"
    patch = paths.execution_patches_dir / f"{task_request.task_id}.diff"
    report.parent.mkdir(parents=True, exist_ok=True)
    patch.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("report\n", encoding="utf-8")
    patch.write_text("patch\n", encoding="utf-8")
    phase0_ready = bool(task_request.phase0 and success)
    task_request.task["status"] = "prepared" if phase0_ready else ("done" if success else "failed")
    prepared_integration: PreparedIntegration | None = None
    if phase0_ready:
        assert task_request.execution_deadline is not None
        facts = PreparedIntegrationFacts(
            transaction_id="integration_" + "1" * 24,
            native_run_id=task_request.native_run_id,
            task_id=task_request.task_id,
            base_branch=str(task_request.base_branch),
            approved_base_head=str(task_request.base_head),
            task_branch="forge/task-1",
            task_commit="a" * 40,
            prepared_merge_commit="b" * 40,
            prepared_ref="refs/alysis/forge-host-v2/integration_" + "1" * 24,
            prepared_elapsed_ms=100,
            journal_path=(
                paths.execution_integration_dir
                / "transactions"
                / ("integration_" + "1" * 24 + ".json")
            ),
        )
        prepared_integration = PreparedIntegration(
            facts=facts,
            execution_deadline=task_request.execution_deadline,
            bind_guarded_state=lambda: None,
            commit_git=lambda: IntegrationCommitProof(
                base_branch=str(task_request.base_branch),
                approved_base_head=str(task_request.base_head),
                task_commit="a" * 40,
                merge_commit="b" * 40,
                integration_committed_elapsed_ms=100,
            ),
            rollback_git=lambda _proof: None,
            cleanup_prepared_git=lambda: None,
            validate_final_authority=lambda: None,
        )
    return TaskExecutionOutcome(
        task_id=task_request.task_id,
        title=str(task_request.task["title"]),
        status=str(task_request.task["status"]),
        success=bool(success and not phase0_ready),
        exit_code=0 if success else 1,
        summary="accepted" if success else "rejected",
        report_path=report,
        patch_path=patch,
        merge_conflict=False,
        conflict_review_path=None,
        usage=usage or ExecutionUsage(),
        timing=ExecutionTiming(
            elapsed_ms=100 if task_request.phase0 and success else 0,
            timed_out=timed_out,
        ),
        integration_commit_proof=None,
        completion_ready=phase0_ready,
        prepared_integration=prepared_integration,
        completion_consistent=True,
    )


def _passed_verification(paths: RunPaths) -> HostVerificationResult:
    artifact_path = paths.execution_verify_dir / "host-pytest.json"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    data = b'{"status":"PASSED"}\n'
    artifact_path.write_bytes(data)
    artifact = RetainedArtifact(
        path=artifact_path,
        digest=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
    )
    return HostVerificationResult(
        command_id="command.pytest",
        argv=("pytest", "-q"),
        status="PASSED",
        real_execution=True,
        exit_code=0,
        timed_out=False,
        summary="pytest verification passed",
        artifact=artifact,
        started_at="2026-08-21T12:00:00+00:00",
        finished_at="2026-08-21T12:00:01+00:00",
        elapsed_ms=1000,
    )


def _verification_binding() -> HostVerificationBinding:
    return HostVerificationBinding(
        command_id="command.pytest",
        argv=("pytest", "-q"),
    )


def _provider_config(
    *,
    profile_name: str = "host-test",
    base_url: str = "https://api.openai.com/v1",
    model: str = "gpt-4.1",
    api_key_env: str | None = None,
    auth_provider: str | None = None,
) -> AppConfig:
    cfg = AppConfig()
    cfg.extra_fields = {}
    add_profile(
        cfg,
        ProfileSpec(
            name=profile_name,
            base_url=base_url,
            api_key_env=api_key_env,
            auth_provider=auth_provider,
            default_model=model,
        ),
        allow_auth_profile_update=True,
    )
    set_active_profile(cfg, profile_name)
    cfg.model = model
    return cfg


def _phase0_request(paths: RunPaths, **overrides) -> SequentialRunRequest:
    defaults = {
        "phase0": True,
        "git_flow": True,
        "verify_mode": "strict",
        "scope_mode": "strict",
        "base_branch": "main",
        "base_head": "c" * 40,
        "execution_deadline": ExecutionDeadline.from_duration(30.0),
        "max_tasks": 1,
        "max_attempts": 1,
        "required_verification_bindings": (_verification_binding(),),
    }
    defaults.update(overrides)
    return _request(paths, **defaults)


def _merged_branch_facts(request: SequentialTaskRequest) -> BranchFacts:
    return BranchFacts(
        task_branch="forge/task-1",
        base_branch=request.base_branch,
        base_head=request.base_head,
        commit="a" * 40,
        merge_commit="b" * 40,
        merge_first_parent=request.base_head,
        merge_second_parent="a" * 40,
        base_ref_after_merge=request.base_head,
        merge_result="prepared",
        merged=False,
        branch_retained=True,
    )


def _dependencies(
    *,
    paths: RunPaths,
    plan: dict,
    execute_task,
    guard: _Guard | None = None,
    clock: _Clock | None = None,
    load_calls: list[int] | None = None,
    statuses: list[tuple[str, str]] | None = None,
    interrupted: list[str] | None = None,
) -> SequentialRunDependencies:
    selected_guard = guard or _Guard()
    selected_clock = clock or _Clock()

    def load(_paths: RunPaths) -> dict:
        if load_calls is not None:
            load_calls.append(1)
        return plan

    def save(_paths: RunPaths, _plan: dict) -> None:
        return None

    def ensure(run_paths: RunPaths) -> None:
        for directory in (
            run_paths.execution_dir,
            run_paths.execution_reports_dir,
            run_paths.execution_patches_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def mark(_paths: RunPaths, status: str, reason: str, _plan: dict | None) -> None:
        if statuses is not None:
            statuses.append((status, reason))

    def mark_interrupted(_paths: RunPaths, reason: str) -> None:
        if interrupted is not None:
            interrupted.append(reason)

    return SequentialRunDependencies(
        load_plan=load,
        save_plan=save,
        ensure_execution_dirs=ensure,
        acquire_mutation_guard=lambda _paths: selected_guard,
        reconcile_and_validate=lambda _paths, value: value,
        execute_task=execute_task,
        mark_run_status=mark,
        mark_interrupted_if_running=mark_interrupted,
        now_iso=lambda: "2026-08-21T12:00:00+00:00",
        monotonic=selected_clock,
        recover_unsealed_integrations=lambda _paths: None,
        finalize_prepared_success=lambda _paths, _task_id, _proof: None,
    )


def _request(paths: RunPaths, **overrides) -> SequentialRunRequest:
    return replace(
        SequentialRunRequest(native_run_id=paths.run_id, native_paths=paths),
        **overrides,
    )


def _run_phase0(
    request: SequentialRunRequest,
    *,
    dependencies: SequentialRunDependencies,
    registrations: list[PreparedIntegration] | None = None,
) -> SequentialRunOutcome:
    retained = registrations if registrations is not None else []
    return run_sequential_forge(
        request,
        dependencies=dependencies,
        prepared_integration_sink=retained.append,
    )


def test_runs_dependencies_in_order_persists_attempts_and_returns_artifacts(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    plan = _plan()
    guard = _Guard()
    statuses: list[tuple[str, str]] = []
    executed: list[str] = []
    progress: list[SequentialRunProgress] = []

    def execute(request: SequentialTaskRequest) -> TaskExecutionOutcome:
        executed.append(request.task_id)
        assert request.phase0 is False
        assert request.phase0_controls.is_phase0_safe()
        return _outcome(
            request,
            usage=ExecutionUsage(
                model_calls=1,
                input_tokens=10,
                output_tokens=4,
                cost_known=True,
                total_cost_micros=25,
            ),
        )

    result = run_sequential_forge(
        _request(paths, verify_mode="strict"),
        dependencies=_dependencies(
            paths=paths,
            plan=plan,
            execute_task=execute,
            guard=guard,
            statuses=statuses,
        ),
        observer=progress.append,
    )

    assert result.success is True
    assert result.exit_code == 0
    assert result.status == RUN_STATUS_COMPLETED
    assert result.stopped_reason == "completed"
    assert result.remaining_task_ids == ()
    assert executed == ["task-1", "task-2"]
    assert [task["attempts"] for task in plan["tasks"]] == [1, 1]
    assert statuses[0][0] == RUN_STATUS_RUNNING
    assert statuses[-1][0] == RUN_STATUS_COMPLETED
    assert guard.released is True
    assert result.usage.model_calls == 2
    assert result.usage.total_cost_micros == 50
    assert result.completion_consistent is True
    assert result.summary_path is not None and result.summary_path.is_file()
    assert result.run_log_path is not None and result.run_log_path.is_file()
    assert result.run_log_path in result.retained_log_paths
    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    assert summary["counts"] == {"executed": 2, "failed": 0, "succeeded": 2}
    assert summary["parallel"] == 1
    assert [item.kind for item in progress] == [
        "run_started",
        "task_started",
        "task_finished",
        "task_started",
        "task_finished",
        "run_finished",
    ]
    assert progress[0].projected_order == ("task-1", "task-2")


def test_stops_after_first_rejected_task_and_marks_failed(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    plan = _plan()
    statuses: list[tuple[str, str]] = []
    executed: list[str] = []

    def execute(request: SequentialTaskRequest) -> TaskExecutionOutcome:
        executed.append(request.task_id)
        return _outcome(request, success=False)

    result = run_sequential_forge(
        _request(paths),
        dependencies=_dependencies(
            paths=paths,
            plan=plan,
            execute_task=execute,
            statuses=statuses,
        ),
    )

    assert executed == ["task-1"]
    assert result.success is False
    assert result.exit_code == 1
    assert result.status == RUN_STATUS_FAILED
    assert result.stopped_reason == "task_failed"
    assert result.remaining_task_ids == ("task-1", "task-2")
    assert statuses[-1][0] == RUN_STATUS_FAILED


def test_keep_going_recomputes_scheduler_and_runs_independent_task(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    plan = _plan()
    plan["tasks"][1]["dependencies"] = []
    executed: list[str] = []

    def execute(request: SequentialTaskRequest) -> TaskExecutionOutcome:
        executed.append(request.task_id)
        return _outcome(request, success=request.task_id == "task-2")

    result = run_sequential_forge(
        _request(paths, keep_going=True),
        dependencies=_dependencies(paths=paths, plan=plan, execute_task=execute),
    )

    assert executed == ["task-1", "task-2"]
    assert result.status == RUN_STATUS_FAILED
    assert result.exit_code == 1
    assert len(result.task_outcomes) == 2


def test_post_wait_plan_is_reloaded_and_guard_released(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    plan = _plan()
    guard = _Guard(acquired_after_wait=True)
    load_calls: list[int] = []

    result = run_sequential_forge(
        _request(paths, dry_run=True),
        dependencies=_dependencies(
            paths=paths,
            plan=plan,
            execute_task=lambda request: _outcome(request),
            guard=guard,
            load_calls=load_calls,
        ),
    )

    assert result.stopped_reason == "dry_run"
    assert len(load_calls) == 2
    assert guard.released is True
    assert result.summary_path is None
    assert result.success is False
    assert result.completion_consistent is False


def test_phase0_passes_closed_controls_for_exactly_one_task(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    plan = _plan()
    plan["tasks"] = plan["tasks"][:1]
    seen: list[SequentialTaskRequest] = []
    registrations: list[PreparedIntegration] = []
    guard = _Guard()

    def execute(request: SequentialTaskRequest) -> TaskExecutionOutcome:
        seen.append(request)
        return replace(
            _outcome(request, usage=ExecutionUsage(model_calls=1)),
            verification=(_passed_verification(request.native_paths),),
            branch_facts=_merged_branch_facts(request),
        )

    result = _run_phase0(
        _phase0_request(paths),
        dependencies=_dependencies(
            paths=paths,
            plan=plan,
            execute_task=execute,
            guard=guard,
        ),
        registrations=registrations,
    )

    assert result.success is False
    assert result.completion_ready is True
    assert result.completion_consistent is True
    assert result.integration_commit_proof is None
    assert result.prepared_integration is registrations[0]
    assert result.elapsed_ms == 100
    assert result.stopped_reason == "prepared_for_commit"
    assert result.status == RUN_STATUS_RUNNING
    assert guard.released is False
    assert len(seen) == 1
    assert seen[0].phase0 is True
    assert seen[0].phase0_controls.is_phase0_safe()
    registrations[0].abort("test cleanup")
    assert guard.released is True


def test_phase0_prepared_timing_does_not_claim_a_commit_after_deadline_expiry(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    plan = _plan()
    plan["tasks"] = plan["tasks"][:1]
    clock = _Clock(0.0)
    deadline = ExecutionDeadline.from_duration(1.0, clock=clock)

    def execute(request: SequentialTaskRequest) -> TaskExecutionOutcome:
        outcome = replace(
            _outcome(request, usage=ExecutionUsage(model_calls=1)),
            verification=(_passed_verification(request.native_paths),),
            branch_facts=_merged_branch_facts(request),
        )
        # The exact task has already crossed its atomic integration commit
        # point.  Reporting/translation time must not produce the impossible
        # combination "timed out, but merged".
        clock.value = 2.0
        return outcome

    registrations: list[PreparedIntegration] = []
    result = _run_phase0(
        _phase0_request(paths, execution_deadline=deadline),
        dependencies=_dependencies(
            paths=paths,
            plan=plan,
            execute_task=execute,
            clock=clock,
        ),
        registrations=registrations,
    )

    assert deadline.is_exhausted() is True
    assert result.success is False
    assert result.completion_ready is True
    assert result.timed_out is False
    assert result.elapsed_ms == 100
    assert result.integration_commit_proof is None
    with pytest.raises(engine_mod.PreparedIntegrationError, match="deadline exhausted"):
        registrations[0].commit_reversible()
    assert registrations[0].state == "ROLLED_BACK"


def test_phase0_missing_exact_verification_is_persisted_as_rejected(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    plan = _plan()
    plan["tasks"] = plan["tasks"][:1]

    result = _run_phase0(
        _phase0_request(paths),
        dependencies=_dependencies(
            paths=paths,
            plan=plan,
            execute_task=lambda request: replace(
                _outcome(request, usage=ExecutionUsage(model_calls=1)),
                branch_facts=_merged_branch_facts(request),
            ),
        ),
    )

    assert result.exit_code == 1
    assert result.success is False
    assert result.status == RUN_STATUS_FAILED
    assert result.task_outcomes[0].status == "verify_failed"
    assert result.task_outcomes[0].completion_consistent is False
    assert any(
        "no exact verification" in issue for issue in result.task_outcomes[0].completion_issues
    )


def test_phase0_rejects_tampered_verification_artifact(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    plan = _plan()
    plan["tasks"] = plan["tasks"][:1]

    def execute(request: SequentialTaskRequest) -> TaskExecutionOutcome:
        verification = _passed_verification(request.native_paths)
        verification.artifact.path.write_text("tampered\n", encoding="utf-8")
        return replace(
            _outcome(request, usage=ExecutionUsage(model_calls=1)),
            verification=(verification,),
            branch_facts=_merged_branch_facts(request),
        )

    result = _run_phase0(
        _phase0_request(paths),
        dependencies=_dependencies(paths=paths, plan=plan, execute_task=execute),
    )

    assert result.success is False
    assert result.task_outcomes[0].status == "verify_failed"
    assert any(
        "artifact is unavailable or changed" in issue
        for issue in result.task_outcomes[0].completion_issues
    )


def test_phase0_rejects_passing_result_with_unapproved_argv(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    plan = _plan()
    plan["tasks"] = plan["tasks"][:1]

    def execute(request: SequentialTaskRequest) -> TaskExecutionOutcome:
        verification = replace(_passed_verification(request.native_paths), argv=("pytest",))
        return replace(
            _outcome(request, usage=ExecutionUsage(model_calls=1)),
            verification=(verification,),
            branch_facts=_merged_branch_facts(request),
        )

    result = _run_phase0(
        _phase0_request(paths),
        dependencies=_dependencies(paths=paths, plan=plan, execute_task=execute),
    )

    assert result.success is False
    assert result.task_outcomes[0].status == "verify_failed"
    assert any(
        "coverage does not match" in issue for issue in result.task_outcomes[0].completion_issues
    )


@pytest.mark.parametrize(
    ("usage", "expected_issue"),
    [
        (ExecutionUsage(), "no observed model call"),
        (
            ExecutionUsage(model_calls=1, usage_complete=False),
            "usage classification is incomplete",
        ),
    ],
)
def test_phase0_rejects_missing_or_incomplete_model_usage(
    tmp_path: Path,
    usage: ExecutionUsage,
    expected_issue: str,
) -> None:
    paths = _paths(tmp_path)
    plan = _plan()
    plan["tasks"] = plan["tasks"][:1]

    def execute(request: SequentialTaskRequest) -> TaskExecutionOutcome:
        return replace(
            _outcome(request, usage=usage),
            verification=(_passed_verification(request.native_paths),),
            branch_facts=_merged_branch_facts(request),
        )

    result = _run_phase0(
        _phase0_request(paths),
        dependencies=_dependencies(paths=paths, plan=plan, execute_task=execute),
    )

    assert result.success is False
    assert result.task_outcomes[0].status == "verify_failed"
    assert any(expected_issue in issue for issue in result.task_outcomes[0].completion_issues)


@pytest.mark.parametrize(
    "override",
    [
        {"git_flow": False},
        {"verify_mode": "off"},
        {"scope_mode": "off"},
        {"execution_deadline": None},
        {"execution_deadline": ExecutionDeadline.from_duration(None)},
        {"dry_run": True},
        {"retry_failed": True},
        {"retry_changes_requested": True},
        {"keep_going": True},
        {"max_tasks": None},
        {"max_tasks": 2},
        {"max_attempts": None},
        {"max_attempts": 2},
        {"base_branch": None},
        {"base_head": None},
    ],
)
def test_phase0_closed_request_policy_rejects_before_side_effects(
    tmp_path: Path,
    override: dict[str, object],
) -> None:
    paths = _paths(tmp_path)
    load_calls: list[int] = []

    with pytest.raises(ValueError, match="Phase 0 execution policy rejected"):
        _run_phase0(
            _phase0_request(paths, **override),
            dependencies=_dependencies(
                paths=paths,
                plan=_plan(),
                execute_task=lambda request: _outcome(request),
                load_calls=load_calls,
            ),
        )

    assert load_calls == []


@pytest.mark.parametrize(
    ("branch_override", "expected_issue"),
    [
        ({"merged": True}, "merged before authoritative Host seal"),
        ({"base_branch": "other"}, "merge base does not match"),
        ({"base_head": "d" * 40}, "base HEAD does not match"),
        ({"task_branch": None}, "task branch is missing"),
        ({"commit": None}, "task commit is missing"),
        ({"merge_commit": None}, "merge commit is missing"),
        ({"merge_first_parent": "d" * 40}, "merge first parent"),
        ({"merge_second_parent": "d" * 40}, "merge second parent"),
        ({"base_ref_after_merge": "d" * 40}, "base ref changed"),
    ],
)
def test_phase0_rejects_incomplete_or_mismatched_merge_facts(
    tmp_path: Path,
    branch_override: dict[str, object],
    expected_issue: str,
) -> None:
    paths = _paths(tmp_path)
    plan = _plan()
    plan["tasks"] = plan["tasks"][:1]

    def execute(request: SequentialTaskRequest) -> TaskExecutionOutcome:
        return replace(
            _outcome(request, usage=ExecutionUsage(model_calls=1)),
            verification=(_passed_verification(request.native_paths),),
            branch_facts=replace(_merged_branch_facts(request), **branch_override),
        )

    result = _run_phase0(
        _phase0_request(paths),
        dependencies=_dependencies(paths=paths, plan=plan, execute_task=execute),
    )

    assert result.success is False
    assert result.task_outcomes[0].status == "verify_failed"
    assert any(expected_issue in issue for issue in result.task_outcomes[0].completion_issues)


def test_phase0_rejects_a_premature_integration_proof(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    plan = _plan()
    plan["tasks"] = plan["tasks"][:1]

    def execute(request: SequentialTaskRequest) -> TaskExecutionOutcome:
        outcome = _outcome(request, usage=ExecutionUsage(model_calls=1))
        return replace(
            outcome,
            verification=(_passed_verification(request.native_paths),),
            branch_facts=_merged_branch_facts(request),
            integration_commit_proof=IntegrationCommitProof(
                base_branch=str(request.base_branch),
                approved_base_head=str(request.base_head),
                task_commit="a" * 40,
                merge_commit="b" * 40,
                integration_committed_elapsed_ms=100,
            ),
        )

    result = _run_phase0(
        _phase0_request(paths),
        dependencies=_dependencies(paths=paths, plan=plan, execute_task=execute),
    )

    assert result.success is False
    assert result.integration_commit_proof is None
    assert result.task_outcomes[0].integration_commit_proof is None
    assert any(
        "integration proof before authoritative Host seal" in issue
        for issue in result.task_outcomes[0].completion_issues
    )


def test_task_execution_error_returns_neutral_error_and_marks_interrupted(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    plan = _plan()
    guard = _Guard()
    interrupted: list[str] = []

    def execute(request: SequentialTaskRequest) -> TaskExecutionOutcome:
        raise SequentialTaskExecutionError(
            f"could not execute {request.task_id}",
            kind="git_state",
            task_id=request.task_id,
        )

    result = run_sequential_forge(
        _request(paths),
        dependencies=_dependencies(
            paths=paths,
            plan=plan,
            execute_task=execute,
            guard=guard,
            interrupted=interrupted,
        ),
    )

    assert result.success is False
    assert result.exit_code == 2
    assert result.status == RUN_STATUS_INTERRUPTED
    assert result.stopped_reason == "task_error"
    assert result.error_kind == "git_state"
    assert result.error_message == "could not execute task-1"
    assert len(interrupted) == 1
    assert guard.released is True
    assert result.summary_path is not None and result.summary_path.is_file()


def test_shared_deadline_stops_before_spawn_and_elapsed_is_monotonic(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    plan = _plan()
    clock = _Clock()
    deadline = ExecutionDeadline.from_duration(1.0, clock=clock)
    clock.value = 11.25
    interrupted: list[str] = []

    result = run_sequential_forge(
        _request(paths, execution_deadline=deadline),
        dependencies=_dependencies(
            paths=paths,
            plan=plan,
            execute_task=lambda _request: pytest.fail("must not execute"),
            clock=clock,
            interrupted=interrupted,
        ),
    )

    assert result.stopped_reason == "deadline_exhausted"
    assert result.timed_out is True
    assert result.elapsed_ms == 1250
    assert result.exit_code == 2
    assert result.status == RUN_STATUS_INTERRUPTED
    assert len(interrupted) == 1


def test_rejects_phase0_capability_expansion_before_any_side_effect(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    unsafe = Phase0ExecutionControls(subagents_enabled=True)

    with pytest.raises(ValueError, match="Phase 0 execution capabilities"):
        run_sequential_forge(
            _request(paths, phase0=True, phase0_controls=unsafe),
            dependencies=_dependencies(
                paths=paths,
                plan=_plan(),
                execute_task=lambda request: _outcome(request),
            ),
        )


def test_rejects_phase0_request_without_exact_verification_binding(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    plan = _plan()
    plan["tasks"] = plan["tasks"][:1]

    with pytest.raises(ValueError, match="requires exact verification"):
        run_sequential_forge(
            _request(paths, phase0=True),
            dependencies=_dependencies(
                paths=paths,
                plan=plan,
                execute_task=lambda request: _outcome(request),
            ),
        )


def test_rejects_phase0_request_with_nonadmitted_verification_binding(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    load_calls: list[int] = []

    with pytest.raises(ValueError, match="not Phase 0 admitted"):
        run_sequential_forge(
            _phase0_request(
                paths,
                required_verification_bindings=(
                    HostVerificationBinding(
                        command_id="command.pytest",
                        argv=("pytest", "-x"),
                    ),
                ),
            ),
            dependencies=_dependencies(
                paths=paths,
                plan=_plan(),
                execute_task=lambda request: _outcome(request),
                load_calls=load_calls,
            ),
        )

    assert load_calls == []


def test_unknown_task_cost_keeps_aggregate_cost_unknown(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    plan = _plan()
    plan["tasks"] = plan["tasks"][:1]

    result = run_sequential_forge(
        _request(paths),
        dependencies=_dependencies(
            paths=paths,
            plan=plan,
            execute_task=lambda request: _outcome(
                request,
                usage=ExecutionUsage(
                    model_calls=1,
                    input_tokens=2,
                    output_tokens=3,
                    cost_known=False,
                    total_cost_micros=None,
                ),
            ),
        ),
    )

    assert result.usage.model_calls == 1
    assert result.usage.cost_known is False
    assert result.usage.total_cost_micros is None


def test_max_tasks_is_cli_success_but_not_completed_run_success(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    plan = _plan()

    result = run_sequential_forge(
        _request(paths, max_tasks=1),
        dependencies=_dependencies(
            paths=paths,
            plan=plan,
            execute_task=lambda request: _outcome(request),
        ),
    )

    assert result.exit_code == 0
    assert result.success is False
    assert result.status != RUN_STATUS_COMPLETED
    assert result.stopped_reason == "max_tasks"
    assert result.remaining_task_ids == ("task-2",)
    assert result.completion_consistent is False
    assert result.summary_payload["success"] is False
    assert result.summary_payload["status"] == result.status


def test_zero_outcome_blocked_plan_is_not_success_or_consistent(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    plan = _plan()
    plan["tasks"][0]["dependencies"] = ["missing-task"]

    result = run_sequential_forge(
        _request(paths),
        dependencies=_dependencies(
            paths=paths,
            plan=plan,
            execute_task=lambda _request: pytest.fail("must not execute"),
        ),
    )

    assert result.exit_code == 0
    assert result.success is False
    assert result.stopped_reason == "blocked"
    assert result.task_outcomes == ()
    assert result.completion_consistent is False


def test_reloads_authoritative_plan_after_every_attempt(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    persisted = _plan()
    load_calls: list[int] = []

    def load(_paths: RunPaths) -> dict:
        load_calls.append(1)
        return persisted

    dependencies = _dependencies(
        paths=paths,
        plan=persisted,
        execute_task=lambda request: _outcome(request),
    )
    dependencies = replace(dependencies, load_plan=load)

    result = run_sequential_forge(_request(paths), dependencies=dependencies)

    assert result.success is True
    assert len(load_calls) == 3  # initial load plus one authoritative reload per task


def test_interrupted_marker_failure_still_releases_guard_and_returns_error(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    plan = _plan()
    guard = _Guard()

    def execute(request: SequentialTaskRequest) -> TaskExecutionOutcome:
        raise SequentialTaskExecutionError("task setup failed", task_id=request.task_id)

    dependencies = _dependencies(
        paths=paths,
        plan=plan,
        execute_task=execute,
        guard=guard,
    )
    dependencies = replace(
        dependencies,
        mark_interrupted_if_running=lambda _paths, _reason: (_ for _ in ()).throw(
            RuntimeError("pointer write failed")
        ),
    )

    result = run_sequential_forge(_request(paths), dependencies=dependencies)

    assert guard.released is True
    assert result.exit_code == 2
    assert result.success is False
    # The primary task error remains authoritative; cleanup failure cannot erase it.
    assert result.stopped_reason == "task_error"
    assert result.error_message == "task setup failed"


def test_run_log_setup_failure_is_returned_without_spawning(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    plan = _plan()
    dependencies = _dependencies(
        paths=paths,
        plan=plan,
        execute_task=lambda _request: pytest.fail("must not execute"),
    )
    dependencies = replace(
        dependencies,
        ensure_execution_dirs=lambda _paths: (_ for _ in ()).throw(
            RuntimeError("execution directory unavailable")
        ),
    )

    result = run_sequential_forge(_request(paths), dependencies=dependencies)

    assert result.exit_code == 2
    assert result.success is False
    assert result.stopped_reason == "engine_error"
    assert result.error_message == "execution directory unavailable"
    assert result.run_log_path is None
    assert result.summary_path is None


def test_observer_failure_cannot_change_execution_outcome(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    plan = _plan()
    plan["tasks"] = plan["tasks"][:1]

    result = run_sequential_forge(
        _request(paths),
        dependencies=_dependencies(
            paths=paths,
            plan=plan,
            execute_task=lambda request: _outcome(request),
        ),
        observer=lambda _progress: (_ for _ in ()).throw(RuntimeError("telemetry failed")),
    )

    assert result.success is True
    assert result.status == RUN_STATUS_COMPLETED


def test_post_execution_wall_clock_and_run_log_failures_cannot_erase_result(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    plan = _plan()
    calls = 0

    def flaky_now() -> str:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("audit clock unavailable")
        return "2026-08-21T12:00:00+00:00"

    dependencies = replace(
        _dependencies(
            paths=paths,
            plan=plan,
            execute_task=lambda request: _outcome(request),
        ),
        now_iso=flaky_now,
    )

    result = run_sequential_forge(_request(paths), dependencies=dependencies)

    assert result.success is True
    assert result.status == RUN_STATUS_COMPLETED
    assert result.task_outcomes
    assert result.finished_at == result.started_at


@pytest.mark.parametrize("classification", ["subscription_calls", "included_calls", "local_calls"])
def test_zero_cost_classifications_are_complete_and_known(
    monkeypatch: pytest.MonkeyPatch,
    classification: str,
) -> None:
    class Summary:
        def totals(self) -> dict[str, object]:
            return {
                "calls": 1,
                "known_cost_calls": 0,
                "unknown_cost_calls": 0,
                "subscription_calls": int(classification == "subscription_calls"),
                "included_calls": int(classification == "included_calls"),
                "local_calls": int(classification == "local_calls"),
                "known_cost_usd": 0.0,
            }

    monkeypatch.setattr(engine_mod, "aggregate_usage_from_session_logs", lambda _paths: Summary())

    usage = usage_from_session_logs([])

    assert usage.cost_known is True
    assert usage.total_cost_micros == 0
    assert usage.usage_complete is True


def test_missing_or_overlapping_cost_classification_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Summary:
        def __init__(self, totals: dict[str, object]) -> None:
            self._totals = totals

        def totals(self) -> dict[str, object]:
            return self._totals

    common: dict[str, object] = {
        "calls": 2,
        "known_cost_calls": 1,
        "unknown_cost_calls": 0,
        "subscription_calls": 0,
        "included_calls": 0,
        "local_calls": 0,
        "known_cost_usd": 0.25,
    }
    monkeypatch.setattr(
        engine_mod,
        "aggregate_usage_from_session_logs",
        lambda _paths: Summary(common),
    )
    assert usage_from_session_logs([]).usage_complete is False

    overlapping = {**common, "calls": 1, "included_calls": 1}
    monkeypatch.setattr(
        engine_mod,
        "aggregate_usage_from_session_logs",
        lambda _paths: Summary(overlapping),
    )
    assert usage_from_session_logs([]).usage_complete is False


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        capture_output=True,
        text=True,
    )


def _phase0_native_task(repo: Path) -> tuple[RunPaths, dict, dict, str]:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    (repo / ".gitignore").write_text("ignored-output.txt\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")
    base_branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    plan["project_goal"] = "Implement one exact Host slice"
    plan["summary"] = plan["project_goal"]
    task = add_task(
        plan,
        title="Add exact feature",
        description="Create one feature module",
        estimated_files=["src/feature.py"],
    )
    task["write_scope"] = ["src/feature.py"]
    save_plan(paths, plan)
    return paths, plan, task, base_branch


def _real_prepared_transaction(
    tmp_path: Path,
    *,
    deadline: ExecutionDeadline | None = None,
    guard: _Guard | None = None,
) -> tuple[
    Path,
    RunPaths,
    PreparedIntegration,
    _Guard,
    str,
    str,
    str,
    dict[Path, bytes | None],
]:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths, plan, task, base_branch = _phase0_native_task(repo)
    with (repo / ".git" / "info" / "exclude").open("a", encoding="utf-8") as handle:
        handle.write(".alysis/\n")
    base_head = _git(repo, "rev-parse", base_branch).stdout.strip()
    task_branch = "forge/recovery-task"
    task["branch"] = task_branch
    save_plan(paths, plan)
    set_current_run_status(
        paths,
        RUN_STATUS_APPROVED,
        reason="approved for transaction test",
        plan=plan,
    )
    ensure_execution_dirs(paths)
    preimage_paths = (
        paths.plan_json_path,
        paths.plan_md_path,
        paths.runtime_dir / "current_run.json",
    )
    before = {path: (path.read_bytes() if path.exists() else None) for path in preimage_paths}
    native_preimage = engine_mod._capture_native_run_state(paths)

    _git(repo, "checkout", "-q", "-b", task_branch, base_head)
    (repo / "src").mkdir()
    (repo / "src" / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "src/feature.py")
    _git(repo, "commit", "-q", "-m", "task")
    task_commit = _git(repo, "rev-parse", "HEAD").stdout.strip()
    task["status"] = "prepared"
    save_plan(paths, plan)
    set_current_run_status(
        paths,
        RUN_STATUS_RUNNING,
        reason="transaction test running",
        plan=plan,
    )
    deadline = deadline or ExecutionDeadline.from_duration(30.0)
    transaction = forge_impl._prepare_new_host_task_integration(
        repo,
        native_run_id=paths.run_id,
        task_id=str(task["id"]),
        journal_dir=paths.execution_integration_dir / "transactions",
        base_branch=base_branch,
        base_head=base_head,
        task_branch=task_branch,
        task_commit=task_commit,
        message="merge recovery task",
        execution_deadline=deadline,
        mutable_runtime_paths=(
            paths.plan_json_path,
            paths.plan_md_path,
            paths.runtime_dir / "current_run.json",
            paths.execution_dir / "sequential_summary.json",
            paths.execution_dir / "sequential_run.jsonl",
        ),
    )
    guard = guard or _Guard()
    transaction._adopt_engine_authority(
        guard=guard,
        native_preimage=native_preimage,
        journal_root=paths.execution_integration_dir / "transactions",
        finalize_native=lambda proof: forge_impl.finalize_host_prepared_success(
            paths,
            str(task["id"]),
            proof,
        ),
    )
    (paths.execution_dir / "sequential_summary.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "command": "forge.run",
                "run_id": paths.run_id,
                "status": RUN_STATUS_RUNNING,
                "stopped_reason": "prepared_for_commit",
                "success": False,
                "exit_code": 0,
                "timed_out": False,
                "completion_ready": True,
                "executed": [
                    {
                        "task_id": str(task["id"]),
                        "status": "prepared",
                        "success": False,
                        "merge_result": "prepared",
                        "completion_consistent": True,
                    }
                ],
                "counts": {"executed": 1, "succeeded": 0, "failed": 1},
                "remaining": [],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (paths.execution_dir / "sequential_run.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-08-21T12:00:00+00:00",
                "event": "run_finished",
                "status": RUN_STATUS_RUNNING,
                "stopped_reason": "prepared_for_commit",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return (
        repo,
        paths,
        transaction,
        guard,
        base_branch,
        base_head,
        task_commit,
        before,
    )


def test_prepared_transaction_stays_reversible_until_authoritative_publish(
    tmp_path: Path,
) -> None:
    repo, paths, transaction, guard, base_branch, base_head, task_commit, _before = (
        _real_prepared_transaction(tmp_path)
    )

    commit_proof = transaction.commit_reversible()
    assert transaction.state == "COMMITTED_REVERSIBLE"
    assert commit_proof.approved_base_head == base_head
    assert _git(repo, "rev-parse", base_branch).stdout.strip() == commit_proof.merge_commit
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == ("forge/recovery-task")
    assert guard.released is False

    finalize_proof = transaction.finalize_reversible()
    assert transaction.state == "PUBLISH_READY"
    assert finalize_proof.commit_proof == commit_proof
    assert finalize_proof.final_elapsed_ms >= commit_proof.integration_committed_elapsed_ms
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == base_branch
    assert load_plan(paths)["tasks"][0]["status"] == "done"
    assert current_run_status(repo) == RUN_STATUS_COMPLETED
    assert guard.released is False

    published = b'{"type":"run.terminal","status":"succeeded"}\n'
    sealed = transaction.complete_publish(
        event_bytes_digest=hashlib.sha256(published).hexdigest(),
        event_bytes=len(published),
    )
    assert sealed == finalize_proof
    assert transaction.state == "SEALED"
    assert guard.released is True
    assert _git(repo, "rev-parse", base_branch).stdout.strip() == commit_proof.merge_commit
    assert _git(repo, "rev-parse", f"{commit_proof.merge_commit}^2").stdout.strip() == task_commit
    journal = json.loads(transaction.facts.journal_path.read_text(encoding="utf-8"))
    assert journal["state"] == "SEALED"
    assert journal["authoritativePublication"] == {
        "eventBytes": len(published),
        "eventBytesDigest": hashlib.sha256(published).hexdigest(),
    }
    assert len(journal["nativePostimage"]["files"]) == 5
    summary = json.loads(
        (paths.execution_dir / "sequential_summary.json").read_text(encoding="utf-8")
    )
    assert summary["status"] == RUN_STATUS_COMPLETED
    assert summary["stopped_reason"] == "completed"
    assert summary["success"] is True
    assert summary["completion_ready"] is False
    assert summary["completion_consistent"] is True
    assert summary["executed"][0]["status"] == "done"
    log_records = [
        json.loads(line)
        for line in (paths.execution_dir / "sequential_run.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert log_records[-1]["event"] == "host_sealed"
    assert log_records[-1]["status"] == RUN_STATUS_COMPLETED


def test_sealed_journal_write_crossing_deadline_stays_reversible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(0.0)
    deadline = ExecutionDeadline.from_duration(1.0, clock=clock)
    repo, paths, transaction, guard, base_branch, base_head, _task_commit, before = (
        _real_prepared_transaction(tmp_path, deadline=deadline)
    )
    transaction.commit_reversible()
    finalize_proof = transaction.finalize_reversible()
    native_atomic_write = engine_mod.atomic_write_text
    sealed_writes = 0

    def delayed_seal(path: Path, text: str) -> None:
        nonlocal sealed_writes
        native_atomic_write(path, text)
        if '"state": "SEALED"' in text:
            sealed_writes += 1
            clock.value = 1.1

    monkeypatch.setattr(engine_mod, "atomic_write_text", delayed_seal)
    published = b'{"type":"run.terminal","status":"succeeded"}\n'
    with pytest.raises(
        engine_mod.PreparedIntegrationError,
        match="could not be sealed",
    ):
        transaction.complete_publish(
            event_bytes_digest=hashlib.sha256(published).hexdigest(),
            event_bytes=len(published),
        )

    assert sealed_writes == 1
    assert transaction.state == "PUBLISH_READY"
    assert transaction.integration_proof == finalize_proof.commit_proof
    assert guard.released is False
    journal = json.loads(transaction.facts.journal_path.read_text(encoding="utf-8"))
    assert journal["state"] == "PUBLISH_READY"
    assert "authoritativePublication" not in journal

    transaction.rollback("publication crossed the shared deadline")
    assert transaction.state == "ROLLED_BACK"
    assert guard.released is True
    assert _git(repo, "rev-parse", base_branch).stdout.strip() == base_head
    for path, expected in before.items():
        assert (path.read_bytes() if path.exists() else None) == expected


def test_sealed_guard_release_failure_can_be_retried(tmp_path: Path) -> None:
    class FlakyGuard(_Guard):
        def __init__(self) -> None:
            super().__init__()
            self.release_calls = 0

        def release(self) -> None:
            self.release_calls += 1
            if self.release_calls == 1:
                raise OSError("guard backend temporarily unavailable")
            super().release()

    flaky_guard = FlakyGuard()
    _repo, _paths_value, transaction, _guard, *_rest = _real_prepared_transaction(
        tmp_path,
        guard=flaky_guard,
    )
    transaction.commit_reversible()
    finalize_proof = transaction.finalize_reversible()
    published = b'{"type":"run.terminal","status":"succeeded"}\n'

    assert (
        transaction.complete_publish(
            event_bytes_digest=hashlib.sha256(published).hexdigest(),
            event_bytes=len(published),
        )
        == finalize_proof
    )
    assert transaction.state == "SEALED"
    assert transaction.guard_release_pending is True
    assert flaky_guard.released is False
    assert transaction.retry_guard_release() is True
    assert transaction.guard_release_pending is False
    assert flaky_guard.released is True
    assert flaky_guard.release_calls == 2


def test_final_authority_mutation_cannot_be_sealed(tmp_path: Path) -> None:
    repo, _paths_value, transaction, guard, _base_branch, base_head, *_rest = (
        _real_prepared_transaction(tmp_path)
    )
    transaction.commit_reversible()
    transaction.finalize_reversible()
    _git(repo, "branch", "late-host-side-effect", base_head)
    published = b'{"type":"run.terminal","status":"succeeded"}\n'

    with pytest.raises(
        engine_mod.PreparedIntegrationError,
        match="could not be sealed",
    ):
        transaction.complete_publish(
            event_bytes_digest=hashlib.sha256(published).hexdigest(),
            event_bytes=len(published),
        )

    assert transaction.state == "PUBLISH_READY"
    assert guard.released is False
    assert (
        json.loads(transaction.facts.journal_path.read_text(encoding="utf-8"))["state"]
        == "PUBLISH_READY"
    )


@pytest.mark.parametrize(
    "native_file",
    ["plan_json", "plan_md", "current_run", "summary", "run_log"],
)
def test_native_postimage_drift_cannot_be_sealed(
    tmp_path: Path,
    native_file: str,
) -> None:
    _repo, paths, transaction, guard, _base_branch, _base_head, _task_commit, before = (
        _real_prepared_transaction(tmp_path)
    )
    transaction.commit_reversible()
    transaction.finalize_reversible()
    target = {
        "plan_json": paths.plan_json_path,
        "plan_md": paths.plan_md_path,
        "current_run": paths.runtime_dir / "current_run.json",
        "summary": paths.execution_dir / "sequential_summary.json",
        "run_log": paths.execution_dir / "sequential_run.jsonl",
    }[native_file]
    target.write_text(target.read_text(encoding="utf-8") + "\nlate drift\n", encoding="utf-8")
    published = b'{"type":"run.terminal","status":"succeeded"}\n'

    with pytest.raises(
        engine_mod.PreparedIntegrationError,
        match="could not be sealed",
    ):
        transaction.complete_publish(
            event_bytes_digest=hashlib.sha256(published).hexdigest(),
            event_bytes=len(published),
        )

    assert transaction.state == "PUBLISH_READY"
    assert guard.released is False
    transaction.rollback("native final state drifted before publication")
    assert transaction.state == "ROLLED_BACK"
    assert guard.released is True
    for path, expected in before.items():
        assert path.read_bytes() == expected
    assert not (paths.execution_dir / "sequential_summary.json").exists()
    assert not (paths.execution_dir / "sequential_run.jsonl").exists()


def test_guarded_startup_recovers_a_crashed_reversible_commit(tmp_path: Path) -> None:
    repo, paths, transaction, _guard, base_branch, base_head, task_commit, before = (
        _real_prepared_transaction(tmp_path)
    )
    proof = transaction.commit_reversible()
    assert _git(repo, "rev-parse", base_branch).stdout.strip() == proof.merge_commit

    forge_impl.recover_host_integration_transactions(paths)
    assert _git(repo, "rev-parse", base_branch).stdout.strip() == base_head
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == ("forge/recovery-task")
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == task_commit
    assert (
        _git(repo, "show-ref", "--verify", transaction.facts.prepared_ref, check=False).returncode
        != 0
    )
    for path, expected in before.items():
        assert (path.read_bytes() if path.exists() else None) == expected
    journal = json.loads(transaction.facts.journal_path.read_text(encoding="utf-8"))
    assert journal["state"] == "ROLLED_BACK"
    assert journal["manualAttention"] is False
    forge_impl.recover_host_integration_transactions(paths)


@pytest.mark.parametrize(
    "tamper",
    [
        "native_path",
        "ancestor_symlink",
        "workspace_root",
        "task_id",
        "task_branch",
        "merge_parents",
    ],
)
def test_recovery_rejects_forged_journal_before_any_restore_or_cas(
    tmp_path: Path,
    tamper: str,
) -> None:
    repo, paths, transaction, _guard, base_branch, base_head, task_commit, _before = (
        _real_prepared_transaction(tmp_path)
    )
    journal_path = transaction.facts.journal_path
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    if tamper == "native_path":
        payload["nativePreimage"]["files"][0]["relativePath"] = "README.md"
    elif tamper == "ancestor_symlink":
        moved_plan_dir = paths.plan_dir.with_name("plan-moved")
        paths.plan_dir.rename(moved_plan_dir)
        paths.plan_dir.symlink_to(moved_plan_dir, target_is_directory=True)
    elif tamper == "workspace_root":
        payload["nativePreimage"]["workspaceRoot"] = os.fspath(tmp_path)
    elif tamper == "task_id":
        payload["taskId"] = "forged-task"
    elif tamper == "task_branch":
        forged_branch = "forge/forged-recovery-task"
        _git(repo, "branch", forged_branch, task_commit)
        payload["taskBranch"] = forged_branch
    else:
        task_tree = _git(repo, "show", "-s", "--format=%T", task_commit).stdout.strip()
        forged_merge = _git(
            repo,
            "commit-tree",
            task_tree,
            "-p",
            task_commit,
            "-p",
            base_head,
            "-m",
            "forged merge parents",
        ).stdout.strip()
        _git(
            repo,
            "update-ref",
            transaction.facts.prepared_ref,
            forged_merge,
            transaction.facts.prepared_merge_commit,
        )
        payload["preparedMergeCommit"] = forged_merge
    journal_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    native_paths = (
        paths.plan_json_path,
        paths.plan_md_path,
        paths.runtime_dir / "current_run.json",
    )
    native_before = {path: path.read_bytes() for path in native_paths}
    base_before = _git(repo, "rev-parse", base_branch).stdout.strip()
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    symbolic_before = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    prepared_before = _git(
        repo,
        "rev-parse",
        transaction.facts.prepared_ref,
    ).stdout.strip()
    journal_before = journal_path.read_bytes()

    with pytest.raises(forge_impl.GitOpsError):
        forge_impl.recover_host_integration_transactions(paths)

    assert _git(repo, "rev-parse", base_branch).stdout.strip() == base_before
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == symbolic_before
    assert _git(repo, "rev-parse", transaction.facts.prepared_ref).stdout.strip() == prepared_before
    assert journal_path.read_bytes() == journal_before
    for path, expected in native_before.items():
        assert path.read_bytes() == expected


def test_recovery_rejects_a_live_private_ref_for_a_sealed_journal(
    tmp_path: Path,
) -> None:
    repo, paths, transaction, _guard, base_branch, _base_head, _task_commit, _before = (
        _real_prepared_transaction(tmp_path)
    )
    proof = transaction.commit_reversible()
    transaction.finalize_reversible()
    published = b'{"type":"run.terminal","status":"succeeded"}\n'
    transaction.complete_publish(
        event_bytes_digest=hashlib.sha256(published).hexdigest(),
        event_bytes=len(published),
    )
    _git(repo, "update-ref", transaction.facts.prepared_ref, proof.merge_commit)

    with pytest.raises(
        forge_impl.GitOpsError,
        match="terminal Host transaction retained an impossible private ref",
    ):
        forge_impl.recover_host_integration_transactions(paths)

    assert _git(repo, "rev-parse", base_branch).stdout.strip() == proof.merge_commit
    assert (
        _git(repo, "rev-parse", transaction.facts.prepared_ref).stdout.strip() == proof.merge_commit
    )


def test_sealed_recovery_journal_is_inert_after_legitimate_base_advance(
    tmp_path: Path,
) -> None:
    repo, paths, transaction, _guard, base_branch, _base_head, _task_commit, _before = (
        _real_prepared_transaction(tmp_path)
    )
    transaction.commit_reversible()
    transaction.finalize_reversible()
    published = b'{"type":"run.terminal","status":"succeeded"}\n'
    transaction.complete_publish(
        event_bytes_digest=hashlib.sha256(published).hexdigest(),
        event_bytes=len(published),
    )
    (repo / "later.txt").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-q", "-m", "later legitimate commit")
    later_head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    forge_impl.recover_host_integration_transactions(paths)

    assert _git(repo, "rev-parse", base_branch).stdout.strip() == later_head
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == later_head


def test_sealed_recovery_journal_is_inert_after_plan_directory_recreation(
    tmp_path: Path,
) -> None:
    repo, paths, transaction, _guard, base_branch, _base_head, _task_commit, _before = (
        _real_prepared_transaction(tmp_path)
    )
    proof = transaction.commit_reversible()
    transaction.finalize_reversible()
    published = b'{"type":"run.terminal","status":"succeeded"}\n'
    transaction.complete_publish(
        event_bytes_digest=hashlib.sha256(published).hexdigest(),
        event_bytes=len(published),
    )
    archived_plan = paths.plan_dir.with_name("plan-archived")
    paths.plan_dir.rename(archived_plan)
    paths.plan_dir.mkdir()

    forge_impl.recover_host_integration_transactions(paths)

    assert _git(repo, "rev-parse", base_branch).stdout.strip() == proof.merge_commit
    assert paths.plan_dir.is_dir()
    assert archived_plan.is_dir()


@pytest.mark.parametrize("failure_point", ["bind", "journal"])
def test_transaction_adoption_failure_restores_native_bytes_and_releases_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths, plan, _task, base_branch = _phase0_native_task(repo)
    set_current_run_status(
        paths,
        RUN_STATUS_APPROVED,
        reason="approved before adoption",
        plan=plan,
    )
    before = {
        path: path.read_bytes()
        for path in (
            paths.plan_json_path,
            paths.plan_md_path,
            paths.runtime_dir / "current_run.json",
        )
    }
    guard = _Guard()
    transaction_holder: list[PreparedIntegration] = []

    def execute(request: SequentialTaskRequest) -> TaskExecutionOutcome:
        outcome = replace(
            _outcome(request, usage=ExecutionUsage(model_calls=1)),
            verification=(_passed_verification(request.native_paths),),
            branch_facts=_merged_branch_facts(request),
        )
        assert outcome.prepared_integration is not None
        transaction_holder.append(outcome.prepared_integration)
        if failure_point == "bind":
            outcome.prepared_integration._bind_guarded_state = lambda: (_ for _ in ()).throw(
                RuntimeError("guarded bind failed")
            )
        return outcome

    native_atomic_write = engine_mod.atomic_write_text

    def maybe_fail_journal(path: Path, text: str) -> None:
        if (
            failure_point == "journal"
            and path.name.startswith("integration_")
            and '"state": "PREPARED"' in text
        ):
            raise OSError("journal write failed")
        native_atomic_write(path, text)

    monkeypatch.setattr(engine_mod, "atomic_write_text", maybe_fail_journal)

    def mark_status(
        run_paths: RunPaths,
        status: str,
        reason: str,
        value: dict | None,
    ) -> None:
        set_current_run_status(run_paths, status, reason=reason, plan=value)

    def mark_interrupted(run_paths: RunPaths, reason: str) -> None:
        if current_run_status(run_paths.root) == RUN_STATUS_RUNNING:
            set_current_run_status(run_paths, RUN_STATUS_INTERRUPTED, reason=reason)

    dependencies = SequentialRunDependencies(
        load_plan=load_plan,
        save_plan=save_plan,
        ensure_execution_dirs=ensure_execution_dirs,
        acquire_mutation_guard=lambda _paths: guard,
        reconcile_and_validate=lambda _paths, value: value,
        execute_task=execute,
        mark_run_status=mark_status,
        mark_interrupted_if_running=mark_interrupted,
        now_iso=lambda: "2026-08-21T12:00:00+00:00",
        recover_unsealed_integrations=lambda _paths: None,
        finalize_prepared_success=lambda _paths, _task_id, _proof: None,
    )
    result = _run_phase0(
        _phase0_request(
            paths,
            base_branch=base_branch,
            base_head=_git(repo, "rev-parse", base_branch).stdout.strip(),
        ),
        dependencies=dependencies,
    )

    assert result.stopped_reason == "engine_error"
    assert result.completion_ready is False
    assert transaction_holder[0].state == "ROLLED_BACK"
    assert guard.released is True
    for path, expected in before.items():
        assert path.read_bytes() == expected
    assert current_run_status(repo) == RUN_STATUS_APPROVED
    journal = json.loads(transaction_holder[0].facts.journal_path.read_text(encoding="utf-8"))
    assert journal["state"] == "ROLLED_BACK"


@pytest.mark.parametrize("failure_mode", ["exhausted", "clock_error"])
def test_host_reversible_commit_rolls_back_every_post_cas_clock_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("# base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "base")
    base_branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    base_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    task_branch = "forge/phase0-task"
    _git(repo, "checkout", "-q", "-b", task_branch)
    (repo / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "feature.py")
    _git(repo, "commit", "-q", "-m", "task")
    task_commit = _git(repo, "rev-parse", "HEAD").stdout.strip()

    class CommitClock:
        value = 0.0
        broken = False

        def __call__(self) -> float:
            if self.broken:
                raise RuntimeError("clock failed after CAS")
            return self.value

    clock = CommitClock()
    deadline = ExecutionDeadline.from_duration(1.0, clock=clock)
    native_run_git = forge_impl._run_host_git_checked

    def tracked_run_git(root: Path, args: list[str], **kwargs):  # type: ignore[no-untyped-def]
        result = native_run_git(root, args, **kwargs)
        if args[:2] == ["update-ref", f"refs/heads/{base_branch}"] and args[-1] == base_head:
            if failure_mode == "exhausted":
                clock.value = 1.1
            else:
                clock.broken = True
        return result

    monkeypatch.setattr(forge_impl, "_run_host_git_checked", tracked_run_git)

    paths = make_run_paths(root=repo, run_id="native-run-1")
    paths.plan_dir.mkdir(parents=True, exist_ok=True)
    paths.execution_dir.mkdir(parents=True, exist_ok=True)
    transaction = forge_impl._prepare_new_host_task_integration(
        repo,
        native_run_id=paths.run_id,
        task_id="task-1",
        journal_dir=paths.execution_integration_dir / "transactions",
        base_branch=base_branch,
        base_head=base_head,
        task_branch=task_branch,
        task_commit=task_commit,
        message="merge task",
        execution_deadline=deadline,
        mutable_runtime_paths=(
            paths.plan_json_path,
            paths.plan_md_path,
            paths.runtime_dir / "current_run.json",
            paths.execution_dir / "sequential_summary.json",
            paths.execution_dir / "sequential_run.jsonl",
        ),
    )
    guard = _Guard()
    transaction._adopt_engine_authority(
        guard=guard,
        native_preimage=engine_mod._capture_native_run_state(paths),
        journal_root=paths.execution_integration_dir / "transactions",
        finalize_native=lambda _proof: None,
    )

    with pytest.raises(
        (engine_mod.PreparedIntegrationError, engine_mod.PreparedIntegrationRollbackError),
        match="commit|rollback",
    ):
        transaction.commit_reversible()

    assert _git(repo, "rev-parse", f"refs/heads/{base_branch}").stdout.strip() == base_head
    assert _git(repo, "rev-parse", f"refs/heads/{task_branch}").stdout.strip() == task_commit
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == task_branch
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == task_commit
    assert transaction.state == "ROLLED_BACK"
    assert guard.released is True


def _write_known_usage_log(*, sessions_dir: Path, session_id: str) -> None:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    event = {
        "type": "llm_usage",
        "payload": {
            "event_type": "llm_usage",
            "timestamp": "2026-08-21T00:00:00Z",
            "role": "forge_exec",
            "requested_model": "test-model",
            "response_model": "test-model",
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "total_tokens": 5,
            "input_cost_per_token": 0.0,
            "output_cost_per_token": 0.0,
            "cost_usd": 0.0,
            "usage_source": "api",
        },
    }
    (sessions_dir / f"{session_id}.jsonl").write_text(
        json.dumps(event) + "\n",
        encoding="utf-8",
    )


def test_host_task_executor_bootstrap_is_narrow_idempotent_and_complete() -> None:
    script = """
import types
from alysis_code import cli
from alysis_code.cli_impl import forge

def unexpected_command(*_args, **_kwargs):
    raise AssertionError("bootstrap must not execute a command")

forge.forge_run = unexpected_command
forge.forge_exec = unexpected_command
first = forge.build_host_task_executor(cli)
second = forge.build_host_task_executor(cli)
assert first is forge.execute_forge_task
assert second is first
assert forge._missing_host_executor_globals() == ()
try:
    forge.build_host_task_executor(types.ModuleType("different_cli"))
except RuntimeError as exc:
    assert "different CLI module object" in str(exc)
else:
    raise AssertionError("different module identity must fail closed")
"""
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    source_root = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (os.fspath(source_root), env.get("PYTHONPATH", "")) if part
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_host_dependency_factory_binds_the_closed_phase0_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    deadline = ExecutionDeadline.from_duration(30.0)
    binding = _verification_binding()
    command = SimpleNamespace(command_id=binding.command_id, argv=list(binding.argv))
    provider = SimpleNamespace(
        provider_binding_ref="provider.binding.1",
        provider="openai",
        model="test-model",
        account_fingerprint="account.test.1",
    )
    request = SimpleNamespace(
        native_run_id=paths.run_id,
        native_paths=paths,
        native_admission=SimpleNamespace(
            paths=paths,
            provider_binding=provider,
            verification_specs=(
                SimpleNamespace(
                    command_id=binding.command_id,
                    argv=binding.argv,
                    working_directory=".",
                ),
            ),
        ),
        plan_ready=SimpleNamespace(
            verification_contract=SimpleNamespace(commands=(command,)),
            execution_admission=SimpleNamespace(per_managed_task_max_steps=7),
        ),
        base_branch="main",
        base_head="a" * 40,
        execution_deadline=deadline,
        pytest_executable=object(),
    )
    captured_executor: dict[str, object] = {}
    verifier_calls: list[dict[str, object]] = []
    sentinel = object()

    def fake_native_executor(**kwargs):  # type: ignore[no-untyped-def]
        captured_executor.update(kwargs)
        assert kwargs["strict_verification_executor"]() == (verifier_calls[-1]["result"],)
        return sentinel

    def fake_verifier(command_id: str, **kwargs):  # type: ignore[no-untyped-def]
        artifact_path = kwargs["artifact_path"]
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("verification\n", encoding="utf-8")
        artifact = RetainedArtifact(
            path=artifact_path,
            digest=hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
            size_bytes=artifact_path.stat().st_size,
        )
        result = HostVerificationResult(
            command_id=command_id,
            argv=binding.argv,
            status="PASSED",
            real_execution=True,
            exit_code=0,
            timed_out=False,
            summary="passed",
            artifact=artifact,
            started_at="2026-08-21T12:00:00+00:00",
            finished_at="2026-08-21T12:00:01+00:00",
            elapsed_ms=1000,
        )
        verifier_calls.append({**kwargs, "result": result})
        return result

    monkeypatch.setattr(forge_impl, "build_host_task_executor", lambda _cli: fake_native_executor)
    monkeypatch.setattr(forge_impl, "run_host_verification", fake_verifier)
    monkeypatch.setattr(forge_impl, "clone_cfg", deepcopy, raising=False)

    class Console:
        def print(self, *_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            return None

    source_environment = {"PATH": "/approved/bin"}
    effective_config = _provider_config(
        model=provider.model,
        api_key_env="HOST_EXPLICIT_KEY",
    )
    effective_config.max_steps = 99
    effective_config.assets.enabled = True
    explicit_api_key = "sk-host-pinned"
    admitted_identity = forge_impl.derive_host_provider_runtime_identity(
        effective_config,
        account_fingerprint=provider.account_fingerprint,
        credential_value=explicit_api_key,
        credential_source="explicit:api-key-stdin",
    )
    runtime = forge_impl.HostExecutionRuntimeBinding(
        effective_config=effective_config,
        console=Console(),
        provider_binding_ref=provider.provider_binding_ref,
        provider=provider.provider,
        model=provider.model,
        account_fingerprint=provider.account_fingerprint,
        admitted_identity=admitted_identity,
        api_key_override=explicit_api_key,
        base_environment=source_environment,
    )
    dependency_factory = forge_impl.build_forge_host_dependency_factory(
        cli_mod,
        runtime_binding_for=lambda value: runtime if value is request else None,
    )
    dependencies = dependency_factory(request, (binding,))
    source_environment["PATH"] = "/mutated/bin"
    task = {"id": "T01", "title": "Implement feature"}
    result = dependencies.execute_task(
        SequentialTaskRequest(
            native_run_id=paths.run_id,
            native_paths=paths,
            plan={"tasks": [task]},
            task=task,
            task_id="T01",
            position=1,
            base_branch="main",
            base_head="a" * 40,
            execution_deadline=deadline,
            phase0=True,
            phase0_controls=Phase0ExecutionControls(),
        )
    )

    assert result is sentinel
    assert captured_executor["effective_mode"] == "auto"
    assert captured_executor["scope_mode"] == "strict"
    assert captured_executor["verify_mode"] == "strict"
    assert captured_executor["verify_repair_budget"] == 0
    assert captured_executor["no_log"] is False
    assert captured_executor["max_steps_provided"] is True
    assert captured_executor["pr"] is True
    assert captured_executor["review"] is False
    assert captured_executor["auto_resolve_conflicts"] is False
    assert captured_executor["strict_disable_remotes"] is True
    assert captured_executor["execution_deadline"] is deadline
    assert captured_executor["strict_per_task_max_steps"] == 7
    assert captured_executor["strict_verification_bindings"] == (binding,)
    assert captured_executor["effective"].assets.enabled is False
    assert captured_executor["run_cfg"].assets.enabled is False
    assert captured_executor["run_cfg"].model == provider.model
    assert captured_executor["run_cfg"].max_steps == 7
    assert captured_executor["api_key_override"] == explicit_api_key
    assert verifier_calls[0]["execution_deadline"] is deadline
    assert verifier_calls[0]["pytest_executable"] is request.pytest_executable
    assert verifier_calls[0]["base_environment"] == {"PATH": "/approved/bin"}
    assert dependencies.recover_unsealed_integrations is (
        forge_impl.recover_host_integration_transactions
    )
    assert dependencies.finalize_prepared_success is forge_impl.finalize_host_prepared_success


def test_host_provider_identity_supports_secret_free_explicit_credentials() -> None:
    cfg = _provider_config(api_key_env="HOST_UNUSED_KEY")
    secret = "sk-host-explicit-secret"

    identity = forge_impl.derive_host_provider_runtime_identity(
        cfg,
        account_fingerprint="account.test.1",
        credential_value=secret,
        credential_source="explicit:api-key-stdin",
    )

    assert identity.provider == "openai"
    assert identity.credential_source == "explicit:api-key-stdin"
    assert identity.credential_fingerprint is not None
    assert secret not in repr(identity)

    subscription_cfg = _provider_config(auth_provider="openai-codex")
    with pytest.raises(RuntimeError, match="could not be resolved"):
        forge_impl.derive_host_provider_runtime_identity(
            subscription_cfg,
            account_fingerprint="account.test.1",
            credential_value=secret,
            credential_source="explicit:api-key-stdin",
        )


def test_host_runtime_binding_repr_and_trace_are_secret_free() -> None:
    api_secret = "sk-host-runtime-secret"
    config_secret = "config-private-marker"
    environment_secret = "environment-private-marker"
    cfg = _provider_config(api_key_env="HOST_UNUSED_KEY")
    cfg.extra_fields["private_marker"] = config_secret
    identity = forge_impl.derive_host_provider_runtime_identity(
        cfg,
        account_fingerprint="account.test.1",
        credential_value=api_secret,
        credential_source="explicit:api-key-stdin",
    )
    binding = forge_impl.HostExecutionRuntimeBinding(
        effective_config=cfg,
        console=SimpleNamespace(print=lambda *_args, **_kwargs: None),
        provider_binding_ref="provider.binding.1",
        provider="openai",
        model="gpt-4.1",
        account_fingerprint="account.test.1",
        admitted_identity=identity,
        api_key_override=api_secret,
        base_environment={"HOST_PRIVATE": environment_secret},
    )

    rendered = repr(binding)
    try:
        raise RuntimeError(f"Host runtime rejected: {binding!r}")
    except RuntimeError as exc:
        rendered_trace = "".join(traceback.format_exception(exc))

    assert "provider_binding_ref='provider.binding.1'" in rendered
    assert "effective_config=" not in rendered
    assert "api_key_override=" not in rendered
    assert "base_environment=" not in rendered
    for secret in (api_secret, config_secret, environment_secret):
        assert secret not in rendered
        assert secret not in rendered_trace


def test_host_dependency_factory_pins_ambient_api_key_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    deadline = ExecutionDeadline.from_duration(30.0)
    binding = _verification_binding()
    provider = SimpleNamespace(
        provider_binding_ref="provider.binding.1",
        provider="openai",
        model="gpt-4.1",
        account_fingerprint="account.test.1",
    )
    request = SimpleNamespace(
        native_run_id=paths.run_id,
        native_paths=paths,
        native_admission=SimpleNamespace(
            paths=paths,
            provider_binding=provider,
            verification_specs=(
                SimpleNamespace(
                    command_id=binding.command_id,
                    argv=binding.argv,
                    working_directory=".",
                ),
            ),
        ),
        plan_ready=SimpleNamespace(
            verification_contract=SimpleNamespace(
                commands=(
                    SimpleNamespace(
                        command_id=binding.command_id,
                        argv=list(binding.argv),
                    ),
                )
            ),
            execution_admission=SimpleNamespace(per_managed_task_max_steps=7),
        ),
        base_branch="main",
        base_head="a" * 40,
        execution_deadline=deadline,
        pytest_executable=object(),
    )
    key_env = "ALYSIS_TEST_HOST_PINNED_KEY"
    key_a = "sk-host-key-a"
    key_b = "sk-host-key-b"
    monkeypatch.setenv(key_env, key_a)
    cfg = _provider_config(api_key_env=key_env)
    admitted_identity = forge_impl.derive_host_provider_runtime_identity(
        cfg,
        account_fingerprint=provider.account_fingerprint,
    )
    runtime = forge_impl.HostExecutionRuntimeBinding(
        effective_config=cfg,
        console=SimpleNamespace(print=lambda *_args, **_kwargs: None),
        provider_binding_ref=provider.provider_binding_ref,
        provider=provider.provider,
        model=provider.model,
        account_fingerprint=provider.account_fingerprint,
        admitted_identity=admitted_identity,
    )
    captured_executor: dict[str, object] = {}
    sentinel = object()

    def fake_native_executor(**kwargs):  # type: ignore[no-untyped-def]
        captured_executor.update(kwargs)
        return sentinel

    monkeypatch.setattr(forge_impl, "build_host_task_executor", lambda _cli: fake_native_executor)
    monkeypatch.setattr(forge_impl, "clone_cfg", deepcopy, raising=False)
    dependencies = forge_impl.build_forge_host_dependency_factory(
        cli_mod,
        runtime_binding_for=lambda value: runtime if value is request else None,
    )(request, (binding,))

    # Ambient authority changes after dependency construction.  Execution must
    # still use the exact value resolved and fingerprinted at that boundary.
    monkeypatch.setenv(key_env, key_b)
    task = {"id": "T01", "title": "Implement feature"}
    assert (
        dependencies.execute_task(
            SequentialTaskRequest(
                native_run_id=paths.run_id,
                native_paths=paths,
                plan={"tasks": [task]},
                task=task,
                task_id="T01",
                position=1,
                base_branch="main",
                base_head="a" * 40,
                execution_deadline=deadline,
                phase0=True,
                phase0_controls=Phase0ExecutionControls(),
            )
        )
        is sentinel
    )
    assert captured_executor["api_key_override"] == key_a
    assert key_a not in repr(admitted_identity)


def test_host_dependency_factory_rejects_subscription_without_bound_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    binding = _verification_binding()
    provider = SimpleNamespace(
        provider_binding_ref="provider.binding.1",
        provider="openai",
        model="gpt-4.1",
        account_fingerprint="account.test.1",
    )
    request = SimpleNamespace(
        native_paths=paths,
        native_admission=SimpleNamespace(
            paths=paths,
            provider_binding=provider,
            verification_specs=(
                SimpleNamespace(
                    command_id=binding.command_id,
                    argv=binding.argv,
                    working_directory=".",
                ),
            ),
        ),
        plan_ready=SimpleNamespace(
            verification_contract=SimpleNamespace(
                commands=(
                    SimpleNamespace(
                        command_id=binding.command_id,
                        argv=list(binding.argv),
                    ),
                )
            ),
            execution_admission=SimpleNamespace(per_managed_task_max_steps=7),
        ),
        execution_deadline=ExecutionDeadline.from_duration(30.0),
    )
    cfg = _provider_config(auth_provider="openai-codex")
    admitted_identity = forge_impl.derive_host_provider_runtime_identity(
        cfg,
        account_fingerprint=provider.account_fingerprint,
    )
    runtime = forge_impl.HostExecutionRuntimeBinding(
        effective_config=cfg,
        console=SimpleNamespace(print=lambda *_args, **_kwargs: None),
        provider_binding_ref=provider.provider_binding_ref,
        provider=provider.provider,
        model=provider.model,
        account_fingerprint=provider.account_fingerprint,
        admitted_identity=admitted_identity,
    )
    monkeypatch.setattr(
        forge_impl,
        "build_host_task_executor",
        lambda _cli: lambda **_kwargs: pytest.fail("executor must not run"),
    )
    monkeypatch.setattr(forge_impl, "clone_cfg", deepcopy, raising=False)

    with pytest.raises(RuntimeError, match="subscription provider execution is unavailable"):
        forge_impl.build_forge_host_dependency_factory(
            cli_mod,
            runtime_binding_for=lambda value: runtime if value is request else None,
        )(request, (binding,))


@pytest.mark.parametrize(
    "drift",
    ["endpoint", "profile", "account", "credential_source", "credential_value"],
)
def test_host_dependency_factory_rejects_provider_referent_drift_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    paths = _paths(tmp_path)
    binding = _verification_binding()
    account = "account.test.1"
    provider = SimpleNamespace(
        provider_binding_ref="provider.binding.1",
        provider="OPENAI",
        model="gpt-4.1",
        account_fingerprint=account,
    )
    command = SimpleNamespace(command_id=binding.command_id, argv=list(binding.argv))
    request = SimpleNamespace(
        native_paths=paths,
        native_admission=SimpleNamespace(
            paths=paths,
            provider_binding=provider,
            verification_specs=(
                SimpleNamespace(
                    command_id=binding.command_id,
                    argv=binding.argv,
                    working_directory=".",
                ),
            ),
        ),
        plan_ready=SimpleNamespace(
            verification_contract=SimpleNamespace(commands=(command,)),
            execution_admission=SimpleNamespace(per_managed_task_max_steps=7),
        ),
        execution_deadline=ExecutionDeadline.from_duration(30.0),
    )
    admitted_cfg = _provider_config(api_key_env="HOST_KEY_A")
    admitted_identity = forge_impl.derive_host_provider_runtime_identity(
        admitted_cfg,
        account_fingerprint=account,
        credential_value="key-a",
        credential_source="explicit:api-key-stdin",
    )
    runtime_cfg = deepcopy(admitted_cfg)
    runtime_account = account
    runtime_key = "key-a"
    if drift == "endpoint":
        runtime_cfg.extra_fields["profiles"]["host-test"]["base_url"] = "https://api.deepseek.com"
    elif drift == "profile":
        add_profile(
            runtime_cfg,
            ProfileSpec(
                name="host-other",
                base_url="https://api.openai.com/v1",
                api_key_env="HOST_KEY_A",
                default_model=provider.model,
            ),
        )
        set_active_profile(runtime_cfg, "host-other")
        runtime_cfg.model = provider.model
    elif drift == "account":
        runtime_account = "account.test.2"
    elif drift == "credential_source":
        runtime_cfg.extra_fields["profiles"]["host-test"]["api_key_env"] = "HOST_KEY_B"
    else:
        runtime_key = "key-b"
    runtime = forge_impl.HostExecutionRuntimeBinding(
        effective_config=runtime_cfg,
        console=SimpleNamespace(print=lambda *_args, **_kwargs: None),
        provider_binding_ref=provider.provider_binding_ref,
        provider=provider.provider,
        model=provider.model,
        account_fingerprint=runtime_account,
        admitted_identity=admitted_identity,
        api_key_override=runtime_key,
    )
    executor_calls: list[object] = []
    monkeypatch.setattr(
        forge_impl,
        "build_host_task_executor",
        lambda _cli: lambda **kwargs: executor_calls.append(kwargs),
    )

    dependency_factory = forge_impl.build_forge_host_dependency_factory(
        cli_mod,
        runtime_binding_for=lambda value: runtime if value is request else None,
    )
    with pytest.raises(RuntimeError, match="provider"):
        dependency_factory(request, (binding,))

    assert executor_calls == []


@pytest.mark.parametrize(
    ("verification_case", "prepare_expected"),
    [
        ("failed", False),
        ("wrong_binding", False),
        ("forged_artifact", False),
        ("missing_artifact", False),
        ("tampered_artifact", False),
        ("nonzero_agent", False),
        ("out_of_scope", False),
        ("missing_usage", False),
        ("incomplete_usage", False),
        ("git_metadata_mutation", False),
        ("base_ref_mutation", False),
        ("ref_move_restore", False),
        ("extra_ref_mutation", False),
        ("git_config_mutation", False),
        ("git_hook_mutation", False),
        ("ignored_workspace_mutation", False),
        ("merge_race", False),
        ("log_mutation", False),
        ("tail_deadline_cross", True),
        ("passed", True),
    ],
)
def test_exact_verifier_runs_before_integration_and_alone_controls_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verification_case: str,
    prepare_expected: bool,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths, plan, task, base_branch = _phase0_native_task(repo)
    base_head = _git(repo, "rev-parse", base_branch).stdout.strip()
    base_tree = _git(repo, "rev-parse", f"{base_head}^{{tree}}").stdout.strip()
    drift_head = _git(
        repo,
        "commit-tree",
        base_tree,
        "-p",
        base_head,
        "-m",
        "external base drift",
    ).stdout.strip()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_REMOTE_SYNC", "off")
    forge_impl._sync_cli_globals(cli_mod)

    observed_max_steps: list[int | None] = []
    observed_inner_verification: list[tuple[object, object]] = []
    observed_agent_context: list[tuple[str, object, object]] = []

    def fake_agent(
        *,
        root: Path,
        instruction: str,
        max_steps: int | None,
        session_log_dir_override: Path,
        session_id_override: str,
        **_kwargs,
    ) -> int:  # type: ignore[no-untyped-def]
        observed_max_steps.append(max_steps)
        observed_inner_verification.append(
            (
                _kwargs.get("verification_enabled"),
                _kwargs.get("authoritative_verification_commands"),
            )
        )
        observed_agent_context.append(
            (
                instruction,
                _kwargs.get("ephemeral_user_messages"),
                _kwargs.get("ephemeral_system_messages"),
            )
        )
        target = (
            root / "src" / ("helper.py" if verification_case == "out_of_scope" else "feature.py")
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("VALUE = 1\n", encoding="utf-8")
        if verification_case != "missing_usage":
            _write_known_usage_log(
                sessions_dir=session_log_dir_override,
                session_id=session_id_override,
            )
        return 1 if verification_case == "nonzero_agent" else 0

    monkeypatch.setattr(forge_impl, "run_agent", fake_agent)
    monkeypatch.setattr(
        forge_impl,
        "run_task_verification",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("the exact Host path must not call the legacy verifier")
        ),
    )
    prepare_calls: list[tuple[str, str]] = []
    integration_clock = _Clock(0.0)
    integration_deadline = ExecutionDeadline.from_duration(30.0, clock=integration_clock)
    native_prepare = forge_impl._prepare_new_host_task_integration

    def tracked_prepare(root: Path, **kwargs):  # type: ignore[no-untyped-def]
        prepare_calls.append((kwargs["base_branch"], kwargs["task_branch"]))
        if verification_case == "merge_race":
            _git(repo, "update-ref", f"refs/heads/{base_branch}", drift_head)
        result = native_prepare(root, **kwargs)
        if verification_case == "tail_deadline_cross":
            integration_clock.value = 31.0
        return result

    monkeypatch.setattr(forge_impl, "_prepare_new_host_task_integration", tracked_prepare)
    cfg = forge_impl.load_config()
    effective = forge_impl.clone_cfg(cfg)
    effective.model = "test-model"
    effective.assets.enabled = False
    run_cfg = forge_impl.clone_cfg(effective)
    run_cfg.assets.enabled = False
    exact_result = _passed_verification(paths)
    approved_artifact_path = exact_result.artifact.path
    if verification_case == "failed":
        exact_result = replace(
            exact_result,
            status="FAILED",
            exit_code=1,
            summary="pytest verification failed",
        )
    elif verification_case == "wrong_binding":
        exact_result = replace(
            exact_result,
            command_id="command.unapproved",
            argv=("pytest",),
        )
    elif verification_case == "forged_artifact":
        outside = tmp_path / "forged-artifact.json"
        outside.write_bytes(exact_result.artifact.path.read_bytes())
        exact_result = replace(
            exact_result,
            artifact=RetainedArtifact(
                path=outside,
                digest=hashlib.sha256(outside.read_bytes()).hexdigest(),
                size_bytes=outside.stat().st_size,
            ),
        )
    elif verification_case == "missing_artifact":
        exact_result.artifact.path.unlink()
    elif verification_case == "tampered_artifact":
        exact_result.artifact.path.write_text("tampered\n", encoding="utf-8")
    if verification_case == "incomplete_usage":
        monkeypatch.setattr(
            forge_impl,
            "usage_from_session_logs",
            lambda _paths: ExecutionUsage(model_calls=1, usage_complete=False),
        )
    verifier_calls: list[str] = []

    def exact_verifier() -> tuple[HostVerificationResult, ...]:
        verifier_calls.append("verify")
        if verification_case == "git_metadata_mutation":
            _git(repo, "commit", "--allow-empty", "-m", "verifier mutation")
        if verification_case == "base_ref_mutation":
            _git(repo, "update-ref", f"refs/heads/{base_branch}", drift_head)
        if verification_case == "ref_move_restore":
            task_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
            _git(repo, "update-ref", f"refs/heads/{base_branch}", task_head, base_head)
            _git(repo, "update-ref", f"refs/heads/{base_branch}", base_head, task_head)
        if verification_case == "extra_ref_mutation":
            _git(repo, "branch", "verifier-side-effect", base_head)
        if verification_case == "git_config_mutation":
            _git(repo, "config", "host.verifierMutation", "true")
        if verification_case == "git_hook_mutation":
            hook = repo / ".git" / "hooks" / "post-merge"
            hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            hook.chmod(0o755)
        if verification_case == "ignored_workspace_mutation":
            (repo / "ignored-output.txt").write_text("hidden mutation\n", encoding="utf-8")
        if verification_case == "log_mutation":
            log_path = paths.execution_logs_dir / f"{task['id']}.jsonl"
            log_path.write_text("tampered\n", encoding="utf-8")
        return (exact_result,)

    outcome = forge_impl.execute_forge_task(
        console=forge_impl._console(),
        events=ForgeEventEmitter(command="forge", enabled=False),
        paths=paths,
        plan=plan,
        task=task,
        task_id=str(task["id"]),
        effective=effective,
        run_cfg=run_cfg,
        effective_mode="auto",
        scope_mode="strict",
        verify_mode="strict",
        verify_commands=["pytest -q"],
        verify_command_source="forge_host_v2_admitted",
        verify_repair_budget=0,
        api_key_override="test-key",
        yes=True,
        no_log=False,
        max_steps_provided=True,
        pr=True,
        review=False,
        base_branch=base_branch,
        keep_branch=True,
        auto_resolve_conflicts=False,
        execution_deadline=integration_deadline,
        strict_verification_executor=exact_verifier,
        strict_verification_bindings=(_verification_binding(),),
        strict_verification_artifact_paths=(approved_artifact_path,),
        strict_base_head=base_head,
        strict_per_task_max_steps=7,
        strict_disable_remotes=True,
    )

    assert observed_max_steps == [7]
    assert observed_inner_verification == [(False, None)]
    assert len(observed_agent_context) == 1
    semantic_instruction, user_context, system_context = observed_agent_context[0]
    assert semantic_instruction == forge_impl._host_authored_task_instruction(task)
    assert str(task["title"]) in semantic_instruction
    assert str(task["description"]) in semantic_instruction
    assert "Instruction budget (tokens)" not in semantic_instruction
    assert isinstance(user_context, list) and len(user_context) == 1
    assert "# Task Context Pack" in user_context[0]
    assert "Instruction budget (tokens)" in user_context[0]
    assert system_context is None
    if verification_case in {
        "nonzero_agent",
        "out_of_scope",
        "missing_usage",
        "incomplete_usage",
    }:
        assert outcome.verification == ()
        assert verifier_calls == []
    else:
        assert outcome.verification == (exact_result,)
        assert verifier_calls == ["verify"]
    assert outcome.success is False
    if prepare_expected:
        assert len(prepare_calls) == 1
        assert outcome.completion_ready is True
        assert outcome.prepared_integration is not None
        assert outcome.prepared_integration.state == "PREPARED"
        assert outcome.branch_facts.merged is False
        assert outcome.completion_consistent is True
        assert outcome.integration_commit_proof is None
        assert outcome.branch_facts.base_head == base_head
        assert outcome.branch_facts.merge_first_parent == base_head
        assert outcome.branch_facts.merge_second_parent == outcome.branch_facts.commit
        assert outcome.branch_facts.base_ref_after_merge == base_head
        assert _git(repo, "show", f"{base_branch}:src/feature.py", check=False).returncode != 0
        assert (
            _git(
                repo,
                "show",
                f"{outcome.prepared_integration.facts.prepared_ref}:src/feature.py",
            ).stdout
            == "VALUE = 1\n"
        )
        if verification_case == "tail_deadline_cross":
            assert integration_deadline.is_exhausted() is True
            assert outcome.timing.timed_out is False
        outcome.prepared_integration.abort("direct executor test cleanup")
        assert outcome.prepared_integration.state == "ROLLED_BACK"
    else:
        assert len(prepare_calls) == (1 if verification_case == "merge_race" else 0)
        assert outcome.completion_ready is False
        assert outcome.prepared_integration is None
        assert outcome.status == (
            "failed"
            if verification_case
            in {
                "nonzero_agent",
                "out_of_scope",
                "missing_usage",
                "incomplete_usage",
                "merge_race",
            }
            else "verify_failed"
        )
        assert outcome.branch_facts.merged is False
        assert (
            _git(
                repo,
                "show",
                f"{base_branch}:src/feature.py",
                check=False,
            ).returncode
            != 0
        )
        if verification_case == "out_of_scope":
            assert task["write_scope"] == ["src/feature.py"]
            assert (
                _git(
                    repo,
                    "show",
                    f"{base_branch}:src/helper.py",
                    check=False,
                ).returncode
                != 0
            )


def test_exact_host_execution_refuses_an_existing_task_branch_before_agent_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths, plan, task, base_branch = _phase0_native_task(repo)
    base_head = _git(repo, "rev-parse", base_branch).stdout.strip()
    task_branch = "forge/precreated-after-approval"
    task["branch"] = task_branch
    save_plan(paths, plan)
    _git(repo, "branch", task_branch, base_branch)
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_REMOTE_SYNC", "off")
    forge_impl._sync_cli_globals(cli_mod)
    agent_calls: list[str] = []
    verifier_calls: list[str] = []
    monkeypatch.setattr(
        forge_impl,
        "run_agent",
        lambda **_kwargs: agent_calls.append("agent") or 0,
    )
    cfg = forge_impl.load_config()
    effective = forge_impl.clone_cfg(cfg)
    effective.model = "test-model"
    effective.assets.enabled = False
    run_cfg = forge_impl.clone_cfg(effective)
    run_cfg.assets.enabled = False

    with pytest.raises(
        forge_impl.ForgeTaskExecutionError,
        match="failed to create new Host task branch",
    ):
        forge_impl.execute_forge_task(
            console=forge_impl._console(),
            events=ForgeEventEmitter(command="forge", enabled=False),
            paths=paths,
            plan=plan,
            task=task,
            task_id=str(task["id"]),
            effective=effective,
            run_cfg=run_cfg,
            effective_mode="auto",
            scope_mode="strict",
            verify_mode="strict",
            verify_commands=["pytest -q"],
            verify_command_source="forge_host_v2_admitted",
            verify_repair_budget=0,
            api_key_override="test-key",
            yes=True,
            no_log=False,
            max_steps_provided=True,
            pr=True,
            review=False,
            base_branch=base_branch,
            keep_branch=True,
            auto_resolve_conflicts=False,
            execution_deadline=ExecutionDeadline.from_duration(30.0),
            strict_verification_executor=lambda: verifier_calls.append("verify") or (),
            strict_verification_bindings=(_verification_binding(),),
            strict_verification_artifact_paths=(paths.execution_verify_dir / "host-pytest.json",),
            strict_base_head=base_head,
            strict_per_task_max_steps=7,
            strict_disable_remotes=True,
        )

    assert agent_calls == []
    assert verifier_calls == []
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == base_branch


def test_host_git_profile_disables_ambient_repository_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("# base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "base")
    base_branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    base_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    marker = repo / "hook-ran.txt"
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text("#!/bin/sh\nprintf hook > hook-ran.txt\n", encoding="utf-8")
    hook.chmod(0o755)
    monkeypatch.setenv("ALYSIS_GIT_HOOKS", "1")

    forge_impl._checkout_new_host_task_branch(
        repo,
        branch="forge/hook-safe",
        base_branch=base_branch,
        base_head=base_head,
        execution_deadline=ExecutionDeadline.from_duration(10.0),
    )

    assert marker.exists() is False
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "forge/hook-safe"


def test_exact_host_execution_rejects_preexisting_ignored_material_before_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths, plan, task, base_branch = _phase0_native_task(repo)
    base_head = _git(repo, "rev-parse", base_branch).stdout.strip()
    (repo / "ignored-output.txt").write_text("user data\n", encoding="utf-8")
    original = (repo / "ignored-output.txt").read_bytes()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_REMOTE_SYNC", "off")
    forge_impl._sync_cli_globals(cli_mod)
    agent_calls: list[str] = []
    monkeypatch.setattr(
        forge_impl,
        "run_agent",
        lambda **_kwargs: agent_calls.append("agent") or 0,
    )
    cfg = forge_impl.load_config()
    effective = forge_impl.clone_cfg(cfg)
    effective.assets.enabled = False
    run_cfg = forge_impl.clone_cfg(effective)
    run_cfg.assets.enabled = False

    with pytest.raises(
        forge_impl.ForgeTaskExecutionError,
        match="preexisting ignored workspace material",
    ):
        forge_impl.execute_forge_task(
            console=forge_impl._console(),
            events=ForgeEventEmitter(command="forge", enabled=False),
            paths=paths,
            plan=plan,
            task=task,
            task_id=str(task["id"]),
            effective=effective,
            run_cfg=run_cfg,
            effective_mode="auto",
            scope_mode="strict",
            verify_mode="strict",
            verify_commands=["pytest -q"],
            verify_command_source="forge_host_v2_admitted",
            verify_repair_budget=0,
            api_key_override="test-key",
            yes=True,
            no_log=False,
            max_steps_provided=True,
            pr=True,
            review=False,
            base_branch=base_branch,
            keep_branch=True,
            execution_deadline=ExecutionDeadline.from_duration(30.0),
            strict_verification_executor=lambda: (),
            strict_verification_bindings=(_verification_binding(),),
            strict_verification_artifact_paths=(paths.execution_verify_dir / "host-pytest.json",),
            strict_base_head=base_head,
            strict_per_task_max_steps=7,
            strict_disable_remotes=True,
        )

    assert agent_calls == []
    assert (repo / "ignored-output.txt").read_bytes() == original
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == base_branch


def test_exact_host_execution_refuses_base_drift_before_agent_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths, plan, task, base_branch = _phase0_native_task(repo)
    approved_base_head = _git(repo, "rev-parse", base_branch).stdout.strip()
    task["branch"] = "forge/approved-task"
    save_plan(paths, plan)
    (repo / "README.md").write_text("# externally changed\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "external base movement")
    current_base_head = _git(repo, "rev-parse", base_branch).stdout.strip()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_REMOTE_SYNC", "off")
    forge_impl._sync_cli_globals(cli_mod)
    agent_calls: list[str] = []
    verifier_calls: list[str] = []
    monkeypatch.setattr(
        forge_impl,
        "run_agent",
        lambda **_kwargs: agent_calls.append("agent") or 0,
    )
    cfg = forge_impl.load_config()
    effective = forge_impl.clone_cfg(cfg)
    effective.model = "test-model"
    effective.assets.enabled = False
    run_cfg = forge_impl.clone_cfg(effective)
    run_cfg.assets.enabled = False

    with pytest.raises(
        forge_impl.ForgeTaskExecutionError,
        match="approved base branch changed before task branch creation",
    ):
        forge_impl.execute_forge_task(
            console=forge_impl._console(),
            events=ForgeEventEmitter(command="forge", enabled=False),
            paths=paths,
            plan=plan,
            task=task,
            task_id=str(task["id"]),
            effective=effective,
            run_cfg=run_cfg,
            effective_mode="auto",
            scope_mode="strict",
            verify_mode="strict",
            verify_commands=["pytest -q"],
            verify_command_source="forge_host_v2_admitted",
            verify_repair_budget=0,
            api_key_override="test-key",
            yes=True,
            no_log=False,
            max_steps_provided=True,
            pr=True,
            review=False,
            base_branch=base_branch,
            keep_branch=True,
            auto_resolve_conflicts=False,
            execution_deadline=ExecutionDeadline.from_duration(30.0),
            strict_verification_executor=lambda: verifier_calls.append("verify") or (),
            strict_verification_bindings=(_verification_binding(),),
            strict_verification_artifact_paths=(paths.execution_verify_dir / "host-pytest.json",),
            strict_base_head=approved_base_head,
            strict_per_task_max_steps=7,
            strict_disable_remotes=True,
        )

    assert approved_base_head != current_base_head
    assert agent_calls == []
    assert verifier_calls == []
    assert (
        _git(repo, "show-ref", "--verify", "refs/heads/forge/approved-task", check=False).returncode
        != 0
    )
    assert _git(repo, "rev-parse", base_branch).stdout.strip() == current_base_head


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"no_log": True}, "session logging must remain enabled"),
        ({"max_steps_provided": False}, "max_steps must be explicitly provided"),
        ({"strict_per_task_max_steps": None}, "admitted per-task max_steps"),
    ],
)
def test_exact_host_execution_rejects_unbound_budget_or_logs_before_branch_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
    message: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths, plan, task, base_branch = _phase0_native_task(repo)
    base_head = _git(repo, "rev-parse", base_branch).stdout.strip()
    task["branch"] = "forge/approved-task"
    save_plan(paths, plan)
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_REMOTE_SYNC", "off")
    forge_impl._sync_cli_globals(cli_mod)
    agent_calls: list[str] = []
    monkeypatch.setattr(
        forge_impl,
        "run_agent",
        lambda **_kwargs: agent_calls.append("agent") or 0,
    )
    cfg = forge_impl.load_config()
    effective = forge_impl.clone_cfg(cfg)
    effective.model = "test-model"
    effective.assets.enabled = False
    run_cfg = forge_impl.clone_cfg(effective)
    run_cfg.assets.enabled = False
    call = {
        "console": forge_impl._console(),
        "events": ForgeEventEmitter(command="forge", enabled=False),
        "paths": paths,
        "plan": plan,
        "task": task,
        "task_id": str(task["id"]),
        "effective": effective,
        "run_cfg": run_cfg,
        "effective_mode": "auto",
        "scope_mode": "strict",
        "verify_mode": "strict",
        "verify_commands": ["pytest -q"],
        "verify_command_source": "forge_host_v2_admitted",
        "verify_repair_budget": 0,
        "api_key_override": "test-key",
        "yes": True,
        "no_log": False,
        "max_steps_provided": True,
        "pr": True,
        "review": False,
        "base_branch": base_branch,
        "keep_branch": True,
        "auto_resolve_conflicts": False,
        "execution_deadline": ExecutionDeadline.from_duration(30.0),
        "strict_verification_executor": lambda: (),
        "strict_verification_bindings": (_verification_binding(),),
        "strict_verification_artifact_paths": (paths.execution_verify_dir / "host-pytest.json",),
        "strict_base_head": base_head,
        "strict_per_task_max_steps": 7,
    }
    call.update(overrides)

    with pytest.raises(forge_impl.ForgeTaskExecutionError, match=message):
        forge_impl.execute_forge_task(**call)

    assert agent_calls == []
    assert (
        _git(repo, "show-ref", "--verify", "refs/heads/forge/approved-task", check=False).returncode
        != 0
    )
