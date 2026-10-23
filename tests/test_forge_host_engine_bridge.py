from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from alysis_code import cli as cli_mod
from alysis_code.agent import session as agent_session_mod
from alysis_code.cli_impl import forge as forge_impl
from alysis_code.config import clone_cfg, load_config
from alysis_code.execution_deadline import ExecutionDeadline
from alysis_code.forge import create_plan_run, ensure_execution_dirs, load_plan, save_plan
from alysis_code.forge_host_engine_bridge import ForgeHostEngineRunner
from alysis_code.forge_host_native_adapter import (
    NativeAdapterRequest,
    ProviderBinding,
    build_native_plan_admission,
    revalidate_native_plan_admission,
)
from alysis_code.forge_host_protocol.models import (
    ApprovalDecision,
    Identity,
    PlannerGoal,
    PlannerInput,
    RequestEnvelope,
    RunStart,
)
from alysis_code.forge_host_service import (
    ForgeHostService,
    HostPlannerOutcome,
    HostSequentialRunRequest,
)
from alysis_code.forge_host_verification import (
    HostVerificationResult,
    RetainedArtifact,
)
from alysis_code.forge_sequential_engine import (
    BranchFacts,
    ExecutionTiming,
    ExecutionUsage,
    IntegrationCommitProof,
    PreparedIntegration,
    PreparedIntegrationFacts,
    SequentialRunDependencies,
    SequentialTaskExecutionError,
    TaskExecutionOutcome,
)
from alysis_code.llm.types import (
    BillingMode,
    LLMResponse,
    LLMUsage,
    ToolCall,
    UsageConfidence,
    UsageContract,
)

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _admission(
    tmp_path: Path,
    *,
    real_pytest: bool = False,
    provider_binding: ProviderBinding | None = None,
):
    root = tmp_path / "repo"
    trusted_pytest = tmp_path / "bin" / "pytest"
    trusted_pytest.parent.mkdir()
    trusted_pytest.write_text(
        (f"#!{sys.executable}\nfrom pytest import console_main\nraise SystemExit(console_main())\n")
        if real_pytest
        else "#!/bin/sh\nexit 0\n",
        encoding="utf-8",
    )
    trusted_pytest.chmod(0o755)
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "base.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_base.py").write_text(
        "from pathlib import Path\n\n"
        "def test_value():\n"
        "    source = Path(__file__).parents[1] / 'src' / 'base.py'\n"
        "    assert source.read_text(encoding='utf-8') == 'VALUE = 1\\n'\n",
        encoding="utf-8",
    )
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "add", "src", "tests")
    _git(root, "commit", "-m", "initial")
    paths = create_plan_run(root)
    plan = load_plan(paths)
    plan.update(
        {
            "project_goal": "Build a feature module.",
            "summary": "Add and verify one scoped module.",
            "requirements": ["Focused tests pass."],
            "planning_constraints": {
                "schema_version": 1,
                "target_roots": [
                    {
                        "path": "src",
                        "kind": "target_root",
                        "reason_code": "user_target_root",
                        "evidence": "The user explicitly selected src.",
                    }
                ],
                "forbidden_roots": [],
                "decoy_roots": [],
                "unrelated_roots": [],
            },
            "tasks": [
                {
                    "id": "T01",
                    "title": "Implement feature",
                    "description": "Create the scoped feature module.",
                    "acceptance_criteria": ["Focused tests pass."],
                    "dependencies": [],
                    "estimated_files": ["src/feature.py"],
                    "write_scope": ["src/feature.py"],
                    "branch": "",
                    "status": "planned",
                    "attempts": 0,
                    "task_kind": "implementation",
                    "task_kind_reason": "derived diagnostic",
                }
            ],
        }
    )
    admission = build_native_plan_admission(
        NativeAdapterRequest(
            paths=paths,
            plan=plan,
            planner_input="Build the scoped feature module in src/feature.py.",
            workspace_ref="workspace_abcdefgh",
            provider_binding=provider_binding
            or ProviderBinding(
                provider_binding_ref="provider_abcdefgh",
                provider="synthetic",
                model="scripted-v1",
            ),
            per_managed_task_max_steps=12,
            run_timeout_ms=60_000,
            verification_commands=["pytest -q"],
            trusted_pytest=trusted_pytest,
        )
    )
    return root, admission


class _Guard:
    acquired_after_wait = False

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state

    def release(self) -> None:
        self.state["guard"] = False


def _runner_request(tmp_path: Path):
    _root, admission = _admission(tmp_path)
    callback_calls: list[str] = []
    guard_state: dict[str, Any] = {"guard": False}

    def reload_under_guard() -> dict[str, object]:
        assert guard_state["guard"] is True
        callback_calls.append("reload")
        return revalidate_native_plan_admission(admission)

    request = HostSequentialRunRequest(
        native_paths=admission.paths,
        native_plan=admission.native_plan_snapshot,
        native_admission=admission,
        plan_ready=admission.plan_ready,
        host_run_id="run_abcdefgh",
        native_run_id=admission.native_run_id,
        operation_id="operation_abcdefgh",
        base_branch=admission.workspace_git.base_branch,
        base_head=admission.workspace_git.base_head,
        pytest_executable=admission.pytest_executable,
        started_at=NOW,
        execution_deadline=ExecutionDeadline.from_duration(60.0),
        reload_native_plan_under_guard=reload_under_guard,
        register_prepared_integration=lambda _value: None,
    )
    return request, callback_calls, guard_state


def _request(payload: object, *, message: str, operation: str) -> RequestEnvelope:
    return RequestEnvelope(
        protocol="alysis.forge-host",
        protocol_version="2.0",
        message_id=f"msg_{message}",
        operation_id=operation,
        sent_at=NOW,
        kind="request",
        request_id=f"req_{message}",
        idempotency_key=f"idem_{message}",
        payload=payload,
    )


def _plan_request() -> RequestEnvelope:
    identity = Identity(
        session_id="ses_abcdefgh",
        generation_id="gen_abcdefgh",
        run_id=None,
        revision=0,
    )
    return _request(
        PlannerInput(
            type="planner.input",
            identity=identity,
            expected_revision=0,
            input=PlannerGoal(kind="goal", text="Build the scoped feature module."),
        ),
        message="planner0001",
        operation="operation_planner01",
    )


def _approval_request(plan_outcome: Any, identity: Identity) -> RequestEnvelope:
    approval = plan_outcome.events[1].payload
    return _request(
        ApprovalDecision(
            type="approval.decision",
            identity=identity,
            expected_revision=identity.revision,
            approval_operation_id=plan_outcome.events[1].operation_id,
            approval_id=approval.approval_id,
            action_fingerprint=approval.action_fingerprint,
            scope_fingerprint=approval.scope.fingerprint,
            expires_at=approval.expires_at,
            decision="allow_once",
        ),
        message="approval001",
        operation="operation_decide01",
    )


def _run_request(admission: Any, identity: Identity) -> RequestEnvelope:
    ready = admission.plan_ready
    return _request(
        RunStart(
            type="run.start",
            identity=identity,
            expected_revision=identity.revision,
            host_plan_fingerprint=ready.host_plan_fingerprint,
            native_approval_fingerprint=ready.native_approval_fingerprint,
            verification_contract_fingerprint=ready.verification_contract.contract_fingerprint,
            execution_admission=ready.execution_admission,
        ),
        message="runstart001",
        operation="operation_runstart1",
    )


def _dependencies(
    request: HostSequentialRunRequest,
    bindings: tuple[Any, ...],
    guard_state: dict[str, Any],
    *,
    passed: bool,
) -> SequentialRunDependencies:
    paths = request.native_paths

    def acquire(_paths):  # type: ignore[no-untyped-def]
        guard_state["guard"] = True
        return _Guard(guard_state)

    def execute(task_request):  # type: ignore[no-untyped-def]
        assert guard_state["guard"] is True
        assert task_request.execution_deadline is request.execution_deadline
        task_request.task["status"] = "prepared" if passed else "verify_failed"
        save_plan(paths, task_request.plan)
        report = paths.execution_reports_dir / "T01.md"
        verify = paths.execution_verify_dir / "T01.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        verify.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("accepted native completion\n", encoding="utf-8")
        verify.write_text("pytest evidence\n", encoding="utf-8")
        report_artifact = RetainedArtifact(
            path=report,
            digest=hashlib.sha256(report.read_bytes()).hexdigest(),
            size_bytes=report.stat().st_size,
        )
        verify_artifact = RetainedArtifact(
            path=verify,
            digest=hashlib.sha256(verify.read_bytes()).hexdigest(),
            size_bytes=verify.stat().st_size,
        )
        verification = HostVerificationResult(
            command_id=bindings[0].command_id,
            argv=bindings[0].argv,
            status="PASSED" if passed else "FAILED",
            real_execution=True,
            exit_code=0 if passed else 1,
            timed_out=False,
            summary="pytest completed",
            artifact=verify_artifact,
            started_at=NOW.isoformat(),
            finished_at=NOW.isoformat(),
            elapsed_ms=1,
        )
        task_commit = "b" * 40
        merge_commit = "c" * 40
        prepared_elapsed_ms = 1
        prepared_integration = None
        if passed:
            task_branch = request.plan_ready.plan_definition.tasks[0].branch
            _git(paths.root, "checkout", "-b", task_branch, request.base_head)
            (paths.root / "src" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
            _git(paths.root, "add", "src/feature.py")
            _git(paths.root, "commit", "-m", "Implement feature")
            task_commit = _git(paths.root, "rev-parse", "HEAD")
            _git(paths.root, "checkout", request.base_branch)
            _git(
                paths.root,
                "merge",
                "--no-ff",
                task_branch,
                "-m",
                "Merge admitted feature",
            )
            merge_commit = _git(paths.root, "rev-parse", "HEAD")
            _git(
                paths.root,
                "update-ref",
                f"refs/heads/{request.base_branch}",
                request.base_head,
                merge_commit,
            )
            _git(paths.root, "reset", "--hard", request.base_head)
            transaction_id = "integration_" + "2" * 24
            prepared_ref = f"refs/alysis/forge-host-v2/{transaction_id}"
            _git(paths.root, "update-ref", prepared_ref, merge_commit)
            prepared_elapsed_ms = max(
                0,
                round(request.execution_deadline.elapsed_seconds() * 1000),
            )
            facts = PreparedIntegrationFacts(
                transaction_id=transaction_id,
                native_run_id=request.native_run_id,
                task_id=task_request.task_id,
                base_branch=request.base_branch,
                approved_base_head=request.base_head,
                task_branch=task_branch,
                task_commit=task_commit,
                prepared_merge_commit=merge_commit,
                prepared_ref=prepared_ref,
                prepared_elapsed_ms=prepared_elapsed_ms,
                journal_path=(
                    paths.execution_integration_dir / "transactions" / f"{transaction_id}.json"
                ),
            )

            def commit_git() -> IntegrationCommitProof:
                _git(
                    paths.root,
                    "update-ref",
                    f"refs/heads/{request.base_branch}",
                    merge_commit,
                    request.base_head,
                )
                return IntegrationCommitProof(
                    base_branch=request.base_branch,
                    approved_base_head=request.base_head,
                    task_commit=task_commit,
                    merge_commit=merge_commit,
                    integration_committed_elapsed_ms=max(
                        0,
                        round(request.execution_deadline.elapsed_seconds() * 1000),
                    ),
                )

            def rollback_git(_proof):  # type: ignore[no-untyped-def]
                current = _git(
                    paths.root,
                    "rev-parse",
                    f"refs/heads/{request.base_branch}",
                )
                if current == merge_commit:
                    _git(
                        paths.root,
                        "update-ref",
                        f"refs/heads/{request.base_branch}",
                        request.base_head,
                        merge_commit,
                    )
                _git(paths.root, "reset", "--hard", request.base_head)
                subprocess.run(
                    ["git", "-C", str(paths.root), "update-ref", "-d", prepared_ref],
                    check=False,
                    capture_output=True,
                )

            def cleanup_prepared_git() -> None:
                _git(paths.root, "update-ref", "-d", prepared_ref)
                _git(paths.root, "reset", "--hard", merge_commit)

            def validate_final_authority() -> None:
                assert (
                    _git(
                        paths.root,
                        "rev-parse",
                        f"refs/heads/{request.base_branch}",
                    )
                    == merge_commit
                )
                assert _git(paths.root, "rev-parse", "HEAD") == merge_commit

            prepared_integration = PreparedIntegration(
                facts=facts,
                execution_deadline=request.execution_deadline,
                bind_guarded_state=lambda: save_plan(paths, task_request.plan),
                commit_git=commit_git,
                rollback_git=rollback_git,
                cleanup_prepared_git=cleanup_prepared_git,
                validate_final_authority=validate_final_authority,
            )
        return TaskExecutionOutcome(
            task_id=task_request.task_id,
            title="Implement feature",
            status="prepared" if passed else "verify_failed",
            success=False,
            exit_code=0 if passed else 1,
            summary="native execution completed",
            report_path=report,
            patch_path=report,
            merge_conflict=False,
            conflict_review_path=None,
            verification=(verification,),
            artifacts=(report_artifact, verify_artifact),
            usage=ExecutionUsage(
                model_calls=1,
                input_tokens=10,
                output_tokens=5,
                cost_known=False,
                total_cost_micros=None,
                usage_complete=True,
            ),
            timing=ExecutionTiming(
                started_at=NOW.isoformat(),
                finished_at=NOW.isoformat(),
                elapsed_ms=prepared_elapsed_ms,
                timed_out=False,
            ),
            branch_facts=BranchFacts(
                task_branch="feat/t01-implement-feature",
                base_branch=request.base_branch,
                base_head=request.base_head,
                commit=task_commit,
                merge_commit=merge_commit,
                merge_first_parent=request.base_head,
                merge_second_parent=task_commit,
                base_ref_after_merge=request.base_head,
                merge_result="prepared" if passed else None,
                merged=False,
                branch_retained=passed,
            ),
            integration_commit_proof=None,
            completion_ready=passed,
            prepared_integration=prepared_integration,
            completion_consistent=passed,
        )

    def _finalize_task(run_paths, task_id):  # type: ignore[no-untyped-def]
        plan = load_plan(run_paths)
        task = next(item for item in plan["tasks"] if item["id"] == task_id)
        task["status"] = "done"
        save_plan(run_paths, plan)

    return SequentialRunDependencies(
        load_plan=load_plan,
        save_plan=save_plan,
        ensure_execution_dirs=ensure_execution_dirs,
        acquire_mutation_guard=acquire,
        reconcile_and_validate=lambda _paths, plan: plan,
        execute_task=execute,
        mark_run_status=lambda *_args: None,
        mark_interrupted_if_running=lambda *_args: None,
        now_iso=lambda: NOW.isoformat(),
        recover_unsealed_integrations=lambda _paths: None,
        finalize_prepared_success=lambda _paths, task_id, _proof: _finalize_task(
            _paths,
            task_id,
        ),
    )


def test_bridge_runs_guarded_engine_and_builds_exact_success(tmp_path: Path) -> None:
    request, callback_calls, guard_state = _runner_request(tmp_path)
    progress = []
    runner = ForgeHostEngineRunner(
        dependency_factory=lambda native_request, bindings: _dependencies(
            native_request,
            bindings,
            guard_state,
            passed=True,
        )
    )

    result = runner(request, progress_sink=progress.append)

    assert callback_calls == ["reload"]
    assert guard_state["guard"] is True
    assert result.prepared_integration is not None
    assert result.prepared_integration.state == "PREPARED"
    assert progress == list(result.progress)
    assert [item.phase for item in result.progress] == [
        "preflight",
        "executing",
        "verifying",
        "finalizing",
    ]
    assert result.terminal.status == "succeeded"
    assert result.terminal.usage.model_calls == 1
    assert result.terminal.task_usage[0].model_steps == 1
    assert result.terminal.workers.model_dump() == {
        "requested": 1,
        "started": 1,
        "succeeded": 1,
        "failed": 0,
    }
    assert result.terminal.acceptance.status == "passed"
    assert result.terminal.verification.status == "passed"
    assert len(result.evidence[0].artifacts) == 2
    artifacts = {item.workspace_relative_path: item for item in result.evidence[0].artifacts}
    report = next(item for path, item in artifacts.items() if path.endswith("T01.md"))
    verification = next(item for path, item in artifacts.items() if path.endswith("T01.json"))
    assert (report.role, report.kind) == ("report", "report")
    assert (verification.role, verification.kind) == ("evidence", "log")
    result.prepared_integration.abort("direct bridge test completed before Host publication")
    assert guard_state["guard"] is False


def test_bridge_returns_truthful_failed_contract_without_claiming_acceptance(
    tmp_path: Path,
) -> None:
    request, callback_calls, guard_state = _runner_request(tmp_path)
    runner = ForgeHostEngineRunner(
        dependency_factory=lambda native_request, bindings: _dependencies(
            native_request,
            bindings,
            guard_state,
            passed=False,
        )
    )

    result = runner(request)

    assert callback_calls == ["reload"]
    assert result.terminal.status == "failed"
    assert result.terminal.acceptance.status == "not_run"
    assert result.terminal.verification.status == "failed"
    assert result.terminal.workers.failed == 1
    assert result.terminal.usage.model_calls == 1
    assert result.terminal.task_usage[0].model_steps == 1


def test_bridge_rejects_unapproved_verification_binding(tmp_path: Path) -> None:
    request, _callback_calls, guard_state = _runner_request(tmp_path)
    command = request.plan_ready.verification_contract.commands[0]
    mutated_contract = request.plan_ready.verification_contract.model_copy(deep=True)
    mutated_contract.commands[0] = command.model_copy(update={"validation_status": "INVALID"})
    mutated = replace(
        request,
        plan_ready=request.plan_ready.model_copy(
            update={"verification_contract": mutated_contract},
            deep=True,
        ),
    )
    runner = ForgeHostEngineRunner(
        dependency_factory=lambda native_request, bindings: _dependencies(
            native_request,
            bindings,
            guard_state,
            passed=True,
        )
    )

    try:
        runner(mutated)
    except Exception as exc:  # the private mutation must fail before any native side effect
        assert "verification" in str(exc).lower() or "contract" in str(exc).lower()
    else:  # pragma: no cover - explicit fail keeps the assertion dependency-free
        raise AssertionError("the bridge accepted an unapproved verification binding")
    assert guard_state["guard"] is False


def test_host_service_bridge_engine_lifecycle_replays_without_second_execution(
    tmp_path: Path,
) -> None:
    _root, admission = _admission(tmp_path)
    guard_state: dict[str, Any] = {"guard": False}
    factory_calls: list[str] = []

    def factory(native_request, bindings):  # type: ignore[no-untyped-def]
        factory_calls.append(native_request.operation_id)
        return _dependencies(
            native_request,
            bindings,
            guard_state,
            passed=True,
        )

    service = ForgeHostService(
        identity=Identity(
            session_id="ses_abcdefgh",
            generation_id="gen_abcdefgh",
            run_id=None,
            revision=0,
        ),
        planner=lambda _request: HostPlannerOutcome(admission=admission),
        sequential_runner=ForgeHostEngineRunner(dependency_factory=factory),
        current_provider_binding=lambda: admission.provider_binding,
        clock=lambda: NOW,
        host_run_id_factory=lambda: "run_abcdefgh",
    )
    plan_outcome = service.handle_request(_plan_request())
    service.handle_request(_approval_request(plan_outcome, service.identity))
    run_request = _run_request(admission, service.identity)

    first = service.handle_request(run_request)
    replay = service.handle_request(run_request)

    assert [event.payload.type for event in first.events] == [
        "run.progress",
        "run.progress",
        "run.progress",
        "run.progress",
        "evidence.ready",
        "run.terminal",
    ]
    assert first.events[-1].payload.result.status == "succeeded"
    assert replay.replayed is True
    assert replay.event_bytes == first.event_bytes
    assert factory_calls == ["operation_runstart1"]
    assert guard_state["guard"] is False
    merged_head = _git(
        admission.workspace_git.git_root,
        "rev-parse",
        f"refs/heads/{admission.workspace_git.base_branch}",
    )
    assert merged_head != admission.workspace_git.base_head
    assert (
        _git(
            admission.workspace_git.git_root,
            "rev-list",
            "--parents",
            "-n",
            "1",
            merged_head,
        ).split()[1]
        == admission.workspace_git.base_head
    )
    assert (admission.workspace_git.workspace_root / "src" / "feature.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 2\n"
    assert load_plan(admission.paths)["tasks"][0]["status"] == "done"
    journals = list((admission.paths.execution_integration_dir / "transactions").glob("*.json"))
    assert len(journals) == 1
    journal = json.loads(journals[0].read_text(encoding="utf-8"))
    assert journal["state"] == "SEALED"
    assert journal["authoritativePublication"]["eventBytesDigest"] == (
        service._publication_digest(first.event_bytes)  # noqa: SLF001
    )


def test_bridge_preserves_started_worker_when_native_task_raises_before_outcome(
    tmp_path: Path,
) -> None:
    request, callback_calls, guard_state = _runner_request(tmp_path)

    def factory(native_request, bindings):  # type: ignore[no-untyped-def]
        dependencies = _dependencies(
            native_request,
            bindings,
            guard_state,
            passed=True,
        )

        def fail_task(task_request):  # type: ignore[no-untyped-def]
            raise SequentialTaskExecutionError(
                "the native provider failed after worker admission",
                kind="provider_failed",
                task_id=task_request.task_id,
            )

        return replace(dependencies, execute_task=fail_task)

    progress = []
    result = ForgeHostEngineRunner(dependency_factory=factory)(
        request,
        progress_sink=progress.append,
    )

    assert callback_calls == ["reload"]
    assert any(item.worker_id is not None for item in progress)
    assert result.terminal.status == "interrupted"
    assert result.terminal.workers.model_dump() == {
        "requested": 1,
        "started": 1,
        "succeeded": 0,
        "failed": 1,
    }
    assert result.terminal.task_usage[0].model_steps == 0


class _ScriptedForgeClient:
    temperature = 0.2
    usage_counts_authoritative = True
    usage_contract = UsageContract(
        response_usage_confidence=UsageConfidence.AUTHORITATIVE,
        billing_mode=BillingMode.METERED_API,
    )

    def __init__(
        self,
        *,
        model: str,
        provider: str,
        protocol: str,
        base_url: str,
    ) -> None:
        self.model = model
        self.provider_key = provider
        self.protocol = protocol
        self.base_url = base_url
        self.calls: list[dict[str, object]] = []
        usage = LLMUsage(
            prompt_tokens=11,
            completion_tokens=7,
            total_tokens=18,
            provider_cost_usd=0.0,
            provider_cost_currency="USD",
            confidence=UsageConfidence.AUTHORITATIVE,
        )
        self._responses = [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tool_write_feature",
                        name="fs_write",
                        arguments={"path": "src/feature.py", "content": "VALUE = 2\n"},
                    )
                ],
                raw={},
                response_model=model,
                usage=usage,
            ),
            LLMResponse(
                content="Implemented src/feature.py; it is ready for the Host verification gate.",
                tool_calls=[],
                raw={},
                response_model=model,
                usage=usage,
            ),
            LLMResponse(
                content=(
                    "Re-read the task: src/feature.py now contains VALUE = 2 and no "
                    "output outside the approved scope was created."
                ),
                tool_calls=[],
                raw={},
                response_model=model,
                usage=usage,
            ),
        ]

    def chat(
        self,
        *,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": list(messages),
                "tools": tools,
                "tool_choice": tool_choice,
                "stream": stream,
                "temperature": temperature,
            }
        )
        if not self._responses:
            raise AssertionError("the real Forge agent loop exceeded the scripted provider budget")
        return self._responses.pop(0)


def test_production_factory_runs_real_executor_pytest_git_and_sealed_host_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_REMOTE_SYNC", "off")
    credential = "synthetic-test-key"
    credential_source = "test:forge-host-e2e"
    account_fingerprint = "acct_test0001"
    effective = clone_cfg(load_config())
    effective.model = "gpt-4.1-mini"
    admitted_identity = forge_impl.derive_host_provider_runtime_identity(
        effective,
        account_fingerprint=account_fingerprint,
        credential_value=credential,
        credential_source=credential_source,
    )
    provider_binding = ProviderBinding(
        provider_binding_ref="provider_abcdefgh",
        provider=admitted_identity.provider,
        model=admitted_identity.model,
        account_fingerprint=account_fingerprint,
    )
    root, admission = _admission(
        tmp_path,
        real_pytest=True,
        provider_binding=provider_binding,
    )
    runtime = forge_impl.HostExecutionRuntimeBinding(
        effective_config=effective,
        console=type(
            "SilentConsole",
            (),
            {
                "print": lambda *_args, **_kwargs: None,
                "rule": lambda *_args, **_kwargs: None,
            },
        )(),
        provider_binding_ref=admission.provider_binding.provider_binding_ref,
        provider=admission.provider_binding.provider,
        model=admission.provider_binding.model,
        account_fingerprint=admission.provider_binding.account_fingerprint,
        admitted_identity=admitted_identity,
        api_key_override=credential,
        base_environment={"PYTHONDONTWRITEBYTECODE": "1"},
    )
    native_dependency_factory = forge_impl.build_forge_host_dependency_factory(
        cli_mod,
        runtime_binding_for=lambda request: (
            runtime
            if request.native_admission.provider_binding == admission.provider_binding
            else None
        ),
    )
    client_factory_calls: list[dict[str, object]] = []
    scripted_clients: list[_ScriptedForgeClient] = []

    def scripted_client_factory(**kwargs: Any) -> _ScriptedForgeClient:
        client_factory_calls.append(dict(kwargs))
        client = _ScriptedForgeClient(
            model=str(kwargs["model"]),
            provider=admitted_identity.provider,
            protocol=admitted_identity.protocol,
            base_url=admitted_identity.normalized_base_url,
        )
        scripted_clients.append(client)
        return client

    monkeypatch.setattr(agent_session_mod, "make_llm_client", scripted_client_factory)
    real_run_agent = forge_impl.run_agent
    agent_calls: list[dict[str, object]] = []

    def observed_agent(**kwargs: Any) -> int:
        agent_calls.append(
            {
                "model": kwargs["cfg"].model,
                "max_steps": kwargs["max_steps"],
                "no_log": kwargs["no_log"],
                "deadline": kwargs["execution_deadline"],
            }
        )
        return int(real_run_agent(**kwargs))

    # The wrapper delegates to the real loop; it observes, but never fabricates,
    # provider calls, tool effects, usage, verification, or terminal facts.
    monkeypatch.setattr(forge_impl, "run_agent", observed_agent)
    dependency_errors: list[str] = []

    def dependency_factory(request, bindings):  # type: ignore[no-untyped-def]
        try:
            return native_dependency_factory(request, bindings)
        except Exception as exc:
            dependency_errors.append(f"{exc.__class__.__name__}: {exc}")
            raise

    service = ForgeHostService(
        identity=Identity(
            session_id="ses_abcdefgh",
            generation_id="gen_abcdefgh",
            run_id=None,
            revision=0,
        ),
        planner=lambda _request: HostPlannerOutcome(admission=admission),
        sequential_runner=ForgeHostEngineRunner(dependency_factory=dependency_factory),
        current_provider_binding=lambda: admission.provider_binding,
        clock=lambda: NOW,
        host_run_id_factory=lambda: "run_abcdefgh",
    )
    plan_outcome = service.handle_request(_plan_request())
    service.handle_request(_approval_request(plan_outcome, service.identity))
    run_request = _run_request(admission, service.identity)

    first = service.handle_request(run_request)
    replay = service.handle_request(run_request)

    assert replay.replayed is True
    assert first.response.payload.duplicate is False
    assert replay.response.payload.duplicate is True
    assert replay.event_bytes == first.event_bytes
    assert dependency_errors == []
    assert len(agent_calls) == 1
    assert agent_calls[0]["model"] == admission.provider_binding.model
    assert agent_calls[0]["max_steps"] == 12
    assert agent_calls[0]["no_log"] is False
    assert len(client_factory_calls) == len(scripted_clients) >= 1
    for actual_client_call in client_factory_calls:
        assert actual_client_call["model"] == admitted_identity.model
        assert actual_client_call["api_key"] == credential
        assert (
            forge_impl.derive_host_provider_runtime_identity(
                actual_client_call["cfg"],
                account_fingerprint=account_fingerprint,
                credential_value=str(actual_client_call["api_key"]),
                credential_source=credential_source,
            )
            == admitted_identity
        )
    active_clients = [client for client in scripted_clients if client.calls]
    assert len(active_clients) == 1
    # One tool turn and one final answer; the optional spec-faithfulness pass only runs
    # while background processes are still alive.
    assert len(active_clients[0].calls) == 2
    terminal = first.events[-1].payload.result
    assert terminal.status == "succeeded"
    assert terminal.exit_code == 0
    assert terminal.usage.model_calls == 2
    assert terminal.usage.input_tokens == 22
    assert terminal.usage.output_tokens == 14
    assert terminal.task_usage[0].model_steps == 2
    evidence = first.events[-2].payload
    verification = [item for item in evidence.evidence if item.role == "verification"]
    assert len(verification) == 1
    assert verification[0].status == "passed"
    assert verification[0].real_execution is True
    assert verification[0].exit_code == 0
    assert verification[0].timed_out is False

    merged_head = _git(root, "rev-parse", "refs/heads/main")
    parents = _git(root, "rev-list", "--parents", "-n", "1", merged_head).split()
    assert parents[1] == admission.workspace_git.base_head
    assert len(parents) == 3
    assert _git(root, "show", f"{merged_head}:src/feature.py") == "VALUE = 2"
    assert load_plan(admission.paths)["tasks"][0]["status"] == "done"
    current_run = json.loads(
        (admission.paths.runtime_dir / "current_run.json").read_text(encoding="utf-8")
    )
    assert current_run["status"] == "completed"
    session_events = [
        json.loads(line)
        for line in (admission.paths.execution_logs_dir / "T01.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    acceptance_payloads = [
        event["payload"] for event in session_events if event.get("type") == "acceptance_contract"
    ]
    assert len(acceptance_payloads) == 1
    assert acceptance_payloads[0]["expectations"] == []
    assert all(
        "Instruction budget" not in criterion["description"] and criterion["commands"] == []
        for criterion in acceptance_payloads[0]["criteria"]
    )
    assert [
        event["payload"]["detail"]
        for event in session_events
        if event.get("type") == "controller_intervention"
        and event["payload"].get("class") == "finalization_checklist"
    ] == []
    summary = json.loads(
        (admission.paths.execution_dir / "sequential_summary.json").read_text(encoding="utf-8")
    )
    assert summary["status"] == "completed"
    assert summary["success"] is True
    assert summary["completion_ready"] is False
    assert summary["completion_consistent"] is True
    run_log = [
        json.loads(line)
        for line in (admission.paths.execution_dir / "sequential_run.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert run_log[-1]["event"] == "host_sealed"
    assert run_log[-1]["merge_commit"] == merged_head
    journals = list((admission.paths.execution_integration_dir / "transactions").glob("*.json"))
    assert len(journals) == 1
    journal = json.loads(journals[0].read_text(encoding="utf-8"))
    assert journal["state"] == "SEALED"
    assert journal["authoritativePublication"]["eventBytesDigest"] == (
        service._publication_digest(first.event_bytes)  # noqa: SLF001
    )
