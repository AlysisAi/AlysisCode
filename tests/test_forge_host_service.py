from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from alysis_code.forge import create_plan_run, load_plan
from alysis_code.forge_host_native_adapter import (
    NativeAdapterRequest,
    ProviderBinding,
    build_native_plan_admission,
    revalidate_native_plan_admission,
)
from alysis_code.forge_host_protocol.models import (
    ApprovalDecision,
    ArtifactRecord,
    CheckResult,
    EvidenceReady,
    EvidenceRecord,
    Identity,
    PlannerGoal,
    PlannerInput,
    RequestEnvelope,
    RunStart,
    TaskExecutionUsage,
    TerminalReason,
    TerminalResult,
    Timing,
    Usage,
    VerificationResult,
    Workers,
)
from alysis_code.forge_host_service import (
    ForgeHostService,
    HostIntegrationCommitProof,
    HostIntegrationFinalizeProof,
    HostPlannerOutcome,
    HostSequentialProgress,
    HostSequentialRunResult,
)

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _admission(tmp_path: Path, *, run_timeout_ms: int = 60_000):
    root = tmp_path / "repo"
    trusted_pytest = tmp_path / "bin" / "pytest"
    trusted_pytest.parent.mkdir()
    trusted_pytest.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    trusted_pytest.chmod(0o755)
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "base.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_base.py").write_text(
        "from src.base import VALUE\n\ndef test_value():\n    assert VALUE == 1\n",
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
            provider_binding=ProviderBinding(
                provider_binding_ref="provider_abcdefgh",
                provider="synthetic",
                model="scripted-v1",
            ),
            per_managed_task_max_steps=12,
            run_timeout_ms=run_timeout_ms,
            verification_commands=["pytest -q"],
            trusted_pytest=trusted_pytest,
        )
    )
    return root, admission


def _request(
    payload: object,
    *,
    message: str,
    operation: str,
    idempotency: str | None = None,
) -> RequestEnvelope:
    return RequestEnvelope(
        protocol="alysis.forge-host",
        protocol_version="2.0",
        message_id=f"msg_{message}",
        operation_id=operation,
        sent_at=NOW,
        kind="request",
        request_id=f"req_{message}",
        idempotency_key=idempotency or f"idem_{message}",
        payload=payload,
    )


class _Planner:
    def __init__(self, admission: Any) -> None:
        self.admission = admission
        self.calls = 0

    def __call__(self, _request: Any) -> HostPlannerOutcome:
        self.calls += 1
        return HostPlannerOutcome(admission=self.admission)


class _Runner:
    def __init__(self, *, bad_digest: bool = False) -> None:
        self.calls = 0
        self.bad_digest = bad_digest

    def __call__(self, request: Any, *, progress_sink: Any = None) -> HostSequentialRunResult:
        self.calls += 1
        guarded_plan = request.reload_native_plan_under_guard()
        assert guarded_plan["tasks"][0]["branch"] == "feat/t01-implement-feature"
        progress = HostSequentialProgress(
            phase="executing",
            summary="Executing the admitted task.",
            task_id="T01",
            step=1,
            total_steps=1,
            task_model_steps=1,
            worker_id="worker_abcdefgh",
        )
        if progress_sink is not None:
            progress_sink(progress)
        root = request.native_paths.root
        task_branch = request.plan_ready.plan_definition.tasks[0].branch
        _git(root, "checkout", "-b", task_branch, request.base_head)
        (root / "src" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
        _git(root, "add", "src/feature.py")
        _git(root, "commit", "-m", "Implement feature")
        task_commit = _git(root, "rev-parse", "HEAD")
        _git(root, "checkout", request.base_branch)
        _git(root, "merge", "--no-ff", task_branch, "-m", "Merge admitted feature")
        merge_commit = _git(root, "rev-parse", "HEAD")
        artifact_path = request.native_paths.execution_logs_dir / "host-verification.txt"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        data = b"pytest passed\n"
        artifact_path.write_bytes(data)
        relpath = artifact_path.relative_to(request.native_paths.root).as_posix()
        digest = "0" * 64 if self.bad_digest else hashlib.sha256(data).hexdigest()
        artifact = ArtifactRecord(
            artifact_id="artifact_abcdefgh",
            role="evidence",
            kind="log",
            status="created",
            workspace_relative_path=relpath,
            digest=digest,
            size_bytes=len(data),
        )
        ready = request.plan_ready
        checks = {check.domain: check for check in ready.verification_contract.checks}
        acceptance = checks["acceptance"]
        verification = checks["verification"]
        acceptance_record = EvidenceRecord(
            evidence_id="evidence_accept01",
            role="acceptance",
            kind="review",
            status="passed",
            check_id=acceptance.check_id,
            task_id="T01",
            requirement_ids=list(acceptance.requirement_ids),
            command_ids=[],
            real_execution=None,
            exit_code=None,
            timed_out=None,
            summary="The scoped acceptance criterion passed.",
            artifact_refs=[artifact.artifact_id],
        )
        verification_record = EvidenceRecord(
            evidence_id="evidence_verify001",
            role="verification",
            kind="test",
            status="passed",
            check_id=verification.check_id,
            task_id="T01",
            requirement_ids=list(verification.requirement_ids),
            command_ids=list(verification.command_ids),
            real_execution=True,
            exit_code=0,
            timed_out=False,
            summary="The admitted pytest command passed.",
            artifact_refs=[artifact.artifact_id],
        )
        evidence = EvidenceReady(
            type="evidence.ready",
            evidence=[acceptance_record, verification_record],
            artifacts=[artifact],
        )
        terminal = TerminalResult(
            host_plan_fingerprint=ready.host_plan_fingerprint,
            native_approval_fingerprint=ready.native_approval_fingerprint,
            verification_contract_fingerprint=ready.verification_contract.contract_fingerprint,
            execution_admission_fingerprint=ready.execution_admission.fingerprint,
            status="succeeded",
            reason=TerminalReason(
                code="completed_successfully",
                message="The admitted Forge run completed successfully.",
                retryable=False,
            ),
            exit_code=0,
            acceptance=VerificationResult(
                status="passed",
                checks=[
                    CheckResult(
                        check_id=acceptance.check_id,
                        task_id="T01",
                        requirement_ids=list(acceptance.requirement_ids),
                        status="passed",
                        summary="Acceptance passed.",
                        evidence_refs=[acceptance_record.evidence_id],
                    )
                ],
            ),
            verification=VerificationResult(
                status="passed",
                checks=[
                    CheckResult(
                        check_id=verification.check_id,
                        task_id="T01",
                        requirement_ids=list(verification.requirement_ids),
                        status="passed",
                        summary="Verification passed.",
                        evidence_refs=[verification_record.evidence_id],
                    )
                ],
            ),
            usage=Usage(
                model_calls=1,
                input_tokens=10,
                output_tokens=5,
                cost_known=False,
                total_cost_micros=None,
            ),
            timing=Timing(started_at=NOW, finished_at=NOW, elapsed_ms=10, timed_out=False),
            workers=Workers(requested=1, started=1, succeeded=1, failed=0),
            task_usage=[TaskExecutionUsage(task_id="T01", model_steps=1)],
            artifact_refs=[artifact.artifact_id],
        )
        return HostSequentialRunResult(
            progress=(progress,),
            evidence=(evidence,),
            terminal=terminal,
            integration_commit_proof=HostIntegrationCommitProof(
                base_branch=request.base_branch,
                approved_base_head=request.base_head,
                task_commit=task_commit,
                merge_commit=merge_commit,
                integration_committed_elapsed_ms=10,
            ),
        )


@dataclass(frozen=True)
class _PreparedFacts:
    transaction_id: str
    native_run_id: str
    task_id: str
    base_branch: str
    approved_base_head: str
    task_branch: str
    task_commit: str
    prepared_merge_commit: str
    prepared_ref: str
    prepared_elapsed_ms: int
    journal_path: Path


class _PreparedTransaction:
    """Small real-Git transaction used to exercise the Host publication boundary."""

    def __init__(
        self,
        *,
        root: Path,
        facts: _PreparedFacts,
        fail_at: str | None = None,
    ) -> None:
        self.root = root
        self.facts = facts
        self.fail_at = fail_at
        self.state = "PREPARED"
        self.calls: list[str] = []
        self.guard_held = True
        self.guard_retry_calls = 0
        self.publication_binding: tuple[str, int] | None = None
        self._commit_proof = HostIntegrationCommitProof(
            base_branch=facts.base_branch,
            approved_base_head=facts.approved_base_head,
            task_commit=facts.task_commit,
            merge_commit=facts.prepared_merge_commit,
            integration_committed_elapsed_ms=10,
        )
        self._finalize_proof = HostIntegrationFinalizeProof(
            commit_proof=self._commit_proof,
            final_elapsed_ms=20,
        )

    @property
    def guard_release_pending(self) -> bool:
        return self.state == "SEALED" and self.guard_held

    def commit_reversible(self) -> HostIntegrationCommitProof:
        self.calls.append("commit")
        assert self.guard_held is True
        assert self.state == "PREPARED"
        if self.fail_at == "commit":
            raise RuntimeError("commit failed")
        _git(
            self.root,
            "update-ref",
            f"refs/heads/{self.facts.base_branch}",
            self.facts.prepared_merge_commit,
            self.facts.approved_base_head,
        )
        self.state = "COMMITTED_REVERSIBLE"
        return self._commit_proof

    def finalize_reversible(self) -> HostIntegrationFinalizeProof:
        self.calls.append("finalize")
        assert self.guard_held is True
        assert self.state == "COMMITTED_REVERSIBLE"
        if self.fail_at == "finalize":
            raise RuntimeError("finalize failed")
        _git(self.root, "reset", "--hard", self.facts.prepared_merge_commit)
        _git(self.root, "update-ref", "-d", self.facts.prepared_ref)
        self.state = "PUBLISH_READY"
        return self._finalize_proof

    def complete_publish(
        self,
        *,
        event_bytes_digest: str,
        event_bytes: int,
    ) -> HostIntegrationFinalizeProof:
        self.calls.append("complete")
        assert self.guard_held is True
        assert self.state == "PUBLISH_READY"
        if self.fail_at == "complete":
            raise RuntimeError("publication seal failed")
        self.publication_binding = (event_bytes_digest, event_bytes)
        self.state = "SEALED"
        if self.fail_at != "guard_release":
            self.guard_held = False
        if self.fail_at == "bad_return":
            return HostIntegrationFinalizeProof(
                commit_proof=self._commit_proof,
                final_elapsed_ms=self._finalize_proof.final_elapsed_ms + 1,
            )
        return self._finalize_proof

    def retry_guard_release(self) -> bool:
        self.calls.append("retry_guard")
        self.guard_retry_calls += 1
        if self.fail_at == "guard_release" and self.guard_retry_calls == 1:
            return False
        self.guard_held = False
        return True

    def rollback(self, reason: str) -> None:
        del reason
        self.calls.append("rollback")
        assert self.guard_held is True
        if self.fail_at == "rollback":
            self.state = "ROLLBACK_FAILED"
            raise RuntimeError("rollback failed")
        current = _git(
            self.root,
            "rev-parse",
            "--verify",
            f"refs/heads/{self.facts.base_branch}^{{commit}}",
        )
        if current == self.facts.prepared_merge_commit:
            _git(
                self.root,
                "update-ref",
                f"refs/heads/{self.facts.base_branch}",
                self.facts.approved_base_head,
                self.facts.prepared_merge_commit,
            )
        _git(self.root, "reset", "--hard", self.facts.approved_base_head)
        subprocess.run(
            ["git", "-C", str(self.root), "update-ref", "-d", self.facts.prepared_ref],
            check=False,
            capture_output=True,
        )
        self.state = "ROLLED_BACK"
        self.guard_held = False

    def abort(self, reason: str) -> None:
        self.calls.append("abort")
        self.rollback(reason)


class _PreparedRunner(_Runner):
    def __init__(self, *, fail_at: str | None = None, bad_digest: bool = False) -> None:
        super().__init__(bad_digest=bad_digest)
        self.fail_at = fail_at
        self.transaction: _PreparedTransaction | None = None

    def __call__(self, request: Any, *, progress_sink: Any = None) -> HostSequentialRunResult:
        result = super().__call__(request, progress_sink=progress_sink)
        proof = result.integration_commit_proof
        assert proof is not None
        root = request.native_paths.root
        transaction_id = "integration_" + "1" * 24
        prepared_ref = f"refs/alysis/forge-host-v2/{transaction_id}"
        _git(root, "update-ref", prepared_ref, proof.merge_commit)
        _git(
            root,
            "update-ref",
            f"refs/heads/{proof.base_branch}",
            proof.approved_base_head,
            proof.merge_commit,
        )
        _git(root, "reset", "--hard", proof.approved_base_head)
        journal_path = (
            request.native_paths.execution_integration_dir
            / "transactions"
            / f"{transaction_id}.json"
        )
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal_path.write_text('{"state":"PREPARED"}\n', encoding="utf-8")
        facts = _PreparedFacts(
            transaction_id=transaction_id,
            native_run_id=request.native_run_id,
            task_id="T01",
            base_branch=proof.base_branch,
            approved_base_head=proof.approved_base_head,
            task_branch=request.plan_ready.plan_definition.tasks[0].branch,
            task_commit=proof.task_commit,
            prepared_merge_commit=proof.merge_commit,
            prepared_ref=prepared_ref,
            prepared_elapsed_ms=5,
            journal_path=journal_path,
        )
        transaction = _PreparedTransaction(root=root, facts=facts, fail_at=self.fail_at)
        assert request.register_prepared_integration is not None
        request.register_prepared_integration(transaction)
        self.transaction = transaction
        return replace(
            result,
            prepared_integration=transaction,
            integration_commit_proof=None,
        )


class _MutatingBeforeGuardRunner:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, request: Any, *, progress_sink: Any = None) -> HostSequentialRunResult:
        self.calls += 1
        changed = load_plan(request.native_paths)
        changed["summary"] = "Mutated between precheck and guard acquisition."
        request.native_paths.plan_json_path.write_text(
            json.dumps(changed),
            encoding="utf-8",
        )
        request.reload_native_plan_under_guard()
        raise AssertionError("guard-held reload must reject the changed native plan")


class _FakeMonotonic:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _RecoveringMonotonic(_FakeMonotonic):
    def __init__(self) -> None:
        super().__init__()
        self.failing = True

    def __call__(self) -> float:
        if self.failing:
            raise RuntimeError("monotonic clock unavailable")
        return super().__call__()


class _AdvancingRunner(_Runner):
    def __init__(self, monotonic: _FakeMonotonic, seconds: float, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.monotonic = monotonic
        self.seconds = seconds

    def __call__(self, request: Any, *, progress_sink: Any = None) -> HostSequentialRunResult:
        result = super().__call__(request, progress_sink=progress_sink)
        self.monotonic.advance(self.seconds)
        return result


class _SwitchableWallClock:
    def __init__(self) -> None:
        self.failing = False

    def __call__(self) -> datetime:
        if self.failing:
            raise RuntimeError("wall clock unavailable")
        return NOW


class _SwitchableMonotonic(_FakeMonotonic):
    def __init__(self) -> None:
        super().__init__()
        self.failing = False

    def __call__(self) -> float:
        if self.failing:
            raise RuntimeError("run monotonic unavailable")
        return super().__call__()


class _BreaksClockAfterSuccessRunner(_Runner):
    def __init__(self, clock: Any) -> None:
        super().__init__()
        self.clock = clock

    def __call__(self, request: Any, *, progress_sink: Any = None) -> HostSequentialRunResult:
        result = super().__call__(request, progress_sink=progress_sink)
        self.clock.failing = True
        return result


class _ThrowsAfterProgressRunner:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, request: Any, *, progress_sink: Any = None) -> HostSequentialRunResult:
        self.calls += 1
        request.reload_native_plan_under_guard()
        progress = HostSequentialProgress(
            phase="executing",
            summary="The worker completed two observed model steps.",
            task_id="T01",
            step=1,
            total_steps=1,
            task_model_steps=2,
            worker_id="worker_abcdefgh",
        )
        assert progress_sink is not None
        progress_sink(progress)
        raise RuntimeError("adapter failed after emitting progress")


class _BreaksClockAfterProgressRunner(_ThrowsAfterProgressRunner):
    def __init__(self, clock: Any) -> None:
        super().__init__()
        self.clock = clock

    def __call__(self, request: Any, *, progress_sink: Any = None) -> HostSequentialRunResult:
        try:
            return super().__call__(request, progress_sink=progress_sink)
        finally:
            self.clock.failing = True


class _ExplodingProgress(HostSequentialProgress):
    def to_payload(self) -> Any:
        raise RuntimeError("progress conversion failed")


class _ThrowsAfterMalformedProgressRunner:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, request: Any, *, progress_sink: Any = None) -> HostSequentialRunResult:
        self.calls += 1
        request.reload_native_plan_under_guard()
        assert progress_sink is not None
        progress_sink(
            HostSequentialProgress(
                phase="executing",
                summary="One valid model step was observed.",
                task_id="T01",
                step=1,
                total_steps=2,
                task_model_steps=1,
                worker_id="worker_abcdefgh",
            )
        )
        progress_sink(
            _ExplodingProgress(
                phase="executing",
                summary="This malformed item must not erase the valid prefix.",
                task_id="T01",
                step=2,
                total_steps=2,
                task_model_steps=2,
                worker_id="worker_abcdefgh",
            )
        )
        raise RuntimeError("adapter failed after malformed progress")


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


def _approve_request(plan_outcome: Any, identity: Identity) -> RequestEnvelope:
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


def test_service_runs_validated_lifecycle_and_replays_bytes(tmp_path: Path) -> None:
    _root, admission = _admission(tmp_path)
    planner = _Planner(admission)
    runner = _Runner()
    service = ForgeHostService(
        identity=Identity(
            session_id="ses_abcdefgh",
            generation_id="gen_abcdefgh",
            run_id=None,
            revision=0,
        ),
        planner=planner,
        sequential_runner=runner,
        current_provider_binding=lambda: admission.provider_binding,
        clock=lambda: NOW,
        host_run_id_factory=lambda: "run_abcdefgh",
    )

    plan_request = _plan_request()
    plan_outcome = service.handle_request(plan_request)
    assert [event.payload.type for event in plan_outcome.events] == [
        "plan.ready",
        "approval.required",
    ]
    replay_request = _request(
        plan_request.payload,
        message="planner0002",
        operation="operation_planner01",
        idempotency="idem_planner_alias01",
    )
    replay = service.handle_request(replay_request)
    assert replay.replayed is True
    assert replay.event_bytes == plan_outcome.event_bytes
    assert planner.calls == 1

    service.handle_request(_approve_request(plan_outcome, service.identity))
    run_outcome = service.handle_request(_run_request(admission, service.identity))

    assert [event.payload.type for event in run_outcome.events] == [
        "run.progress",
        "evidence.ready",
        "run.terminal",
    ]
    assert run_outcome.events[-1].payload.result.status == "succeeded"
    assert service.identity.run_id == "run_abcdefgh"
    assert admission.native_run_id != service.identity.run_id
    assert runner.calls == 1
    assert service.replay() == (*plan_outcome.event_bytes, *run_outcome.event_bytes)


def test_service_rejects_bad_artifact_truth_with_one_failed_terminal(tmp_path: Path) -> None:
    _root, admission = _admission(tmp_path)
    planner = _Planner(admission)
    monotonic = _FakeMonotonic()
    service = ForgeHostService(
        identity=Identity(
            session_id="ses_abcdefgh",
            generation_id="gen_abcdefgh",
            run_id=None,
            revision=0,
        ),
        planner=planner,
        sequential_runner=_AdvancingRunner(monotonic, 0.025, bad_digest=True),
        current_provider_binding=lambda: admission.provider_binding,
        clock=lambda: NOW,
        monotonic=monotonic,
        host_run_id_factory=lambda: "run_abcdefgh",
    )
    plan_outcome = service.handle_request(_plan_request())
    service.handle_request(_approve_request(plan_outcome, service.identity))

    outcome = service.handle_request(_run_request(admission, service.identity))

    terminals = [event for event in outcome.events if event.payload.type == "run.terminal"]
    assert len(terminals) == 1
    assert terminals[0].payload.result.status == "failed"
    assert terminals[0].payload.result.reason.code == "engine_result_invalid"
    terminal = terminals[0].payload.result
    assert terminal.usage.model_calls == 1
    assert terminal.usage.input_tokens == 10
    assert terminal.usage.output_tokens == 5
    assert terminal.usage.cost_known is False
    assert terminal.task_usage[0].model_steps == 1
    assert terminal.workers.requested == 1
    assert terminal.workers.started == 1
    assert terminal.timing.elapsed_ms == 25
    assert terminal.timing.timed_out is False


def test_service_privately_clones_nested_admission_authority(tmp_path: Path) -> None:
    _root, planner_owned = _admission(tmp_path)
    planner = _Planner(planner_owned)
    service = ForgeHostService(
        identity=Identity(
            session_id="ses_abcdefgh",
            generation_id="gen_abcdefgh",
            run_id=None,
            revision=0,
        ),
        planner=planner,
        sequential_runner=_Runner(),
        current_provider_binding=lambda: planner_owned.provider_binding,
        clock=lambda: NOW,
        host_run_id_factory=lambda: "run_abcdefgh",
    )
    plan_outcome = service.handle_request(_plan_request())

    planner_owned.plan_ready.plan_definition.tasks[0].write_scopes.clear()
    planner_owned.native_plan_snapshot["tasks"][0]["branch"] = "feat/attacker-branch"  # type: ignore[index]
    exposed = service.admission
    assert exposed is not None
    exposed.plan_ready.verification_contract.commands[0].argv.append("attacker")
    exposed.native_plan_snapshot["tasks"][0]["branch"] = "feat/getter-mutation"  # type: ignore[index]

    private = service.admission
    assert private is not None
    assert private.plan_ready.plan_definition.tasks[0].write_scopes
    assert private.plan_ready.verification_contract.commands[0].argv == ["pytest", "-q"]
    assert private.native_plan_snapshot["tasks"][0]["branch"] == "feat/t01-implement-feature"  # type: ignore[index]
    service.handle_request(_approve_request(plan_outcome, service.identity))
    outcome = service.handle_request(_run_request(private, service.identity))
    assert outcome.events[-1].payload.result.status == "succeeded"


def test_service_blocks_run_when_trusted_pytest_was_replaced(tmp_path: Path) -> None:
    _root, admission = _admission(tmp_path)
    planner = _Planner(admission)
    runner = _Runner()
    service = ForgeHostService(
        identity=Identity(
            session_id="ses_abcdefgh",
            generation_id="gen_abcdefgh",
            run_id=None,
            revision=0,
        ),
        planner=planner,
        sequential_runner=runner,
        current_provider_binding=lambda: admission.provider_binding,
        clock=lambda: NOW,
        host_run_id_factory=lambda: "run_abcdefgh",
    )
    plan_outcome = service.handle_request(_plan_request())
    service.handle_request(_approve_request(plan_outcome, service.identity))
    admission.pytest_executable.resolved_path.write_text(
        "#!/bin/sh\nexit 1\n",
        encoding="utf-8",
    )

    outcome = service.handle_request(_run_request(admission, service.identity))
    assert runner.calls == 0
    assert service.identity.run_id == "run_abcdefgh"
    assert outcome.events[-1].payload.result.status == "failed"


def test_service_guard_reload_catches_mutation_after_precheck(tmp_path: Path) -> None:
    _root, admission = _admission(tmp_path)
    planner = _Planner(admission)
    runner = _MutatingBeforeGuardRunner()
    service = ForgeHostService(
        identity=Identity(
            session_id="ses_abcdefgh",
            generation_id="gen_abcdefgh",
            run_id=None,
            revision=0,
        ),
        planner=planner,
        sequential_runner=runner,
        current_provider_binding=lambda: admission.provider_binding,
        clock=lambda: NOW,
        host_run_id_factory=lambda: "run_abcdefgh",
    )
    plan_outcome = service.handle_request(_plan_request())
    service.handle_request(_approve_request(plan_outcome, service.identity))

    outcome = service.handle_request(_run_request(admission, service.identity))

    assert runner.calls == 1
    assert outcome.events[-1].payload.result.status == "failed"
    assert outcome.events[-1].payload.result.reason.code == "engine_result_invalid"


def test_service_runner_performs_authoritative_reload_only_after_guard_acquisition(
    tmp_path: Path,
) -> None:
    _root, admission = _admission(tmp_path)
    guard_held = [False]
    revalidation_calls = 0

    def guard_sensitive_revalidator(value: Any) -> dict[str, object]:
        nonlocal revalidation_calls
        revalidation_calls += 1
        if revalidation_calls == 2:
            assert guard_held[0] is True
        return revalidate_native_plan_admission(value)

    class _ProductionStyleGuardedRunner(_Runner):
        def __call__(
            self,
            request: Any,
            *,
            progress_sink: Any = None,
        ) -> HostSequentialRunResult:
            assert guard_held[0] is False
            guard_held[0] = True
            try:
                return super().__call__(request, progress_sink=progress_sink)
            finally:
                guard_held[0] = False

    runner = _ProductionStyleGuardedRunner()
    service = ForgeHostService(
        identity=Identity(
            session_id="ses_abcdefgh",
            generation_id="gen_abcdefgh",
            run_id=None,
            revision=0,
        ),
        planner=_Planner(admission),
        sequential_runner=runner,
        current_provider_binding=lambda: admission.provider_binding,
        admission_revalidator=guard_sensitive_revalidator,
        clock=lambda: NOW,
        host_run_id_factory=lambda: "run_abcdefgh",
    )
    plan_outcome = service.handle_request(_plan_request())
    service.handle_request(_approve_request(plan_outcome, service.identity))

    outcome = service.handle_request(_run_request(admission, service.identity))

    assert outcome.events[-1].payload.result.status == "succeeded"
    assert revalidation_calls == 2
    assert guard_held[0] is False


def test_service_provider_drift_finishes_failed_without_calling_runner(tmp_path: Path) -> None:
    _root, admission = _admission(tmp_path)
    planner = _Planner(admission)
    runner = _Runner()
    current_provider = [admission.provider_binding]
    service = ForgeHostService(
        identity=Identity(
            session_id="ses_abcdefgh",
            generation_id="gen_abcdefgh",
            run_id=None,
            revision=0,
        ),
        planner=planner,
        sequential_runner=runner,
        current_provider_binding=lambda: current_provider[0],
        clock=lambda: NOW,
        host_run_id_factory=lambda: "run_abcdefgh",
    )
    plan_outcome = service.handle_request(_plan_request())
    service.handle_request(_approve_request(plan_outcome, service.identity))
    current_provider[0] = ProviderBinding(
        provider_binding_ref="provider_abcdefgh",
        provider="synthetic",
        model="different-model",
    )

    outcome = service.handle_request(_run_request(admission, service.identity))

    assert runner.calls == 0
    assert outcome.events[-1].payload.result.status == "failed"
    assert service.phase == "terminal"


def test_invalid_host_run_id_does_not_consume_request_and_retry_replays(
    tmp_path: Path,
) -> None:
    _root, admission = _admission(tmp_path)
    runner = _Runner()
    candidates = iter(("run_bad", "run_abcdefgh"))
    service = ForgeHostService(
        identity=Identity(
            session_id="ses_abcdefgh",
            generation_id="gen_abcdefgh",
            run_id=None,
            revision=0,
        ),
        planner=_Planner(admission),
        sequential_runner=runner,
        current_provider_binding=lambda: admission.provider_binding,
        clock=lambda: NOW,
        host_run_id_factory=lambda: next(candidates),
    )
    plan_outcome = service.handle_request(_plan_request())
    service.handle_request(_approve_request(plan_outcome, service.identity))
    request = _run_request(admission, service.identity)
    replay_before = service.replay()

    with pytest.raises(ValueError):
        service.handle_request(request)
    assert service.identity.run_id is None
    assert service.phase == "ready"
    assert service.replay() == replay_before
    assert runner.calls == 0

    outcome = service.handle_request(request)
    replay = service.handle_request(request)
    assert outcome.events[-1].payload.result.status == "succeeded"
    assert replay.replayed is True
    assert replay.event_bytes == outcome.event_bytes
    assert runner.calls == 1


def test_deadline_clock_failure_does_not_bind_or_consume_run_start(tmp_path: Path) -> None:
    _root, admission = _admission(tmp_path)
    runner = _Runner()
    monotonic = _RecoveringMonotonic()
    service = ForgeHostService(
        identity=Identity(
            session_id="ses_abcdefgh",
            generation_id="gen_abcdefgh",
            run_id=None,
            revision=0,
        ),
        planner=_Planner(admission),
        sequential_runner=runner,
        current_provider_binding=lambda: admission.provider_binding,
        clock=lambda: NOW,
        monotonic=monotonic,
        host_run_id_factory=lambda: "run_abcdefgh",
    )
    plan_outcome = service.handle_request(_plan_request())
    service.handle_request(_approve_request(plan_outcome, service.identity))
    request = _run_request(admission, service.identity)
    replay_before = service.replay()

    with pytest.raises(RuntimeError, match="monotonic clock unavailable"):
        service.handle_request(request)
    assert service.phase == "ready"
    assert service.identity.run_id is None
    assert service.replay() == replay_before
    assert runner.calls == 0

    monotonic.failing = False
    outcome = service.handle_request(request)
    replay = service.handle_request(request)
    assert outcome.events[-1].payload.result.status == "succeeded"
    assert replay.replayed is True
    assert replay.event_bytes == outcome.event_bytes
    assert runner.calls == 1


def test_service_commit_point_is_not_reclassified_by_reporting_tail(tmp_path: Path) -> None:
    _root, admission = _admission(tmp_path, run_timeout_ms=1_000)
    monotonic = _FakeMonotonic()
    runner = _AdvancingRunner(monotonic, 1.1)
    service = ForgeHostService(
        identity=Identity(
            session_id="ses_abcdefgh",
            generation_id="gen_abcdefgh",
            run_id=None,
            revision=0,
        ),
        planner=_Planner(admission),
        sequential_runner=runner,
        current_provider_binding=lambda: admission.provider_binding,
        clock=lambda: NOW,
        monotonic=monotonic,
        host_run_id_factory=lambda: "run_abcdefgh",
    )
    plan_outcome = service.handle_request(_plan_request())
    service.handle_request(_approve_request(plan_outcome, service.identity))

    outcome = service.handle_request(_run_request(admission, service.identity))

    terminal = outcome.events[-1].payload.result
    assert runner.calls == 1
    assert terminal.status == "succeeded"
    assert terminal.timing.elapsed_ms == 10
    assert terminal.timing.timed_out is False


def test_service_deadline_includes_preflight_time(tmp_path: Path) -> None:
    _root, admission = _admission(tmp_path, run_timeout_ms=1_000)
    monotonic = _FakeMonotonic()
    runner = _Runner()

    def slow_preflight(value: Any) -> dict[str, object]:
        monotonic.advance(1.1)
        return revalidate_native_plan_admission(value)

    service = ForgeHostService(
        identity=Identity(
            session_id="ses_abcdefgh",
            generation_id="gen_abcdefgh",
            run_id=None,
            revision=0,
        ),
        planner=_Planner(admission),
        sequential_runner=runner,
        current_provider_binding=lambda: admission.provider_binding,
        admission_revalidator=slow_preflight,
        clock=lambda: NOW,
        monotonic=monotonic,
        host_run_id_factory=lambda: "run_abcdefgh",
    )
    plan_outcome = service.handle_request(_plan_request())
    service.handle_request(_approve_request(plan_outcome, service.identity))

    outcome = service.handle_request(_run_request(admission, service.identity))

    terminal = outcome.events[-1].payload.result
    assert runner.calls == 0
    assert terminal.status == "failed"
    assert terminal.timing.elapsed_ms == 1_100
    assert terminal.timing.timed_out is True


def test_service_preserves_canonical_progress_when_runner_raises(tmp_path: Path) -> None:
    _root, admission = _admission(tmp_path)
    runner = _ThrowsAfterProgressRunner()
    service = ForgeHostService(
        identity=Identity(
            session_id="ses_abcdefgh",
            generation_id="gen_abcdefgh",
            run_id=None,
            revision=0,
        ),
        planner=_Planner(admission),
        sequential_runner=runner,
        current_provider_binding=lambda: admission.provider_binding,
        clock=lambda: NOW,
        host_run_id_factory=lambda: "run_abcdefgh",
    )
    plan_outcome = service.handle_request(_plan_request())
    service.handle_request(_approve_request(plan_outcome, service.identity))

    outcome = service.handle_request(_run_request(admission, service.identity))

    assert [event.payload.type for event in outcome.events] == [
        "run.progress",
        "evidence.ready",
        "run.terminal",
    ]
    terminal = outcome.events[-1].payload.result
    assert terminal.status == "failed"
    assert terminal.task_usage[0].model_steps == 2
    assert terminal.workers.requested == 1
    assert terminal.workers.started == 1
    assert terminal.workers.failed == 1


@pytest.mark.parametrize("runner_succeeds", [True, False])
def test_post_commit_wall_clock_failure_still_terminalizes_and_replays(
    tmp_path: Path,
    runner_succeeds: bool,
) -> None:
    _root, admission = _admission(tmp_path)
    wall_clock = _SwitchableWallClock()
    runner = (
        _BreaksClockAfterSuccessRunner(wall_clock)
        if runner_succeeds
        else _BreaksClockAfterProgressRunner(wall_clock)
    )
    service = ForgeHostService(
        identity=Identity(
            session_id="ses_abcdefgh",
            generation_id="gen_abcdefgh",
            run_id=None,
            revision=0,
        ),
        planner=_Planner(admission),
        sequential_runner=runner,
        current_provider_binding=lambda: admission.provider_binding,
        clock=wall_clock,
        host_run_id_factory=lambda: "run_abcdefgh",
    )
    plan_outcome = service.handle_request(_plan_request())
    service.handle_request(_approve_request(plan_outcome, service.identity))
    request = _run_request(admission, service.identity)

    outcome = service.handle_request(request)
    replay = service.handle_request(request)

    assert outcome.events[-1].payload.type == "run.terminal"
    assert outcome.events[-1].payload.result.status == (
        "succeeded" if runner_succeeds else "failed"
    )
    assert replay.replayed is True
    assert replay.event_bytes == outcome.event_bytes
    assert runner.calls == 1
    assert service.phase == "terminal"


@pytest.mark.parametrize("runner_succeeds", [True, False])
def test_consumed_approval_terminal_never_reconsults_failed_shared_wall_clock(
    tmp_path: Path,
    runner_succeeds: bool,
) -> None:
    _root, admission = _admission(tmp_path)
    clock_state = {"failing": False}

    def shared_wall_clock() -> datetime:
        if clock_state["failing"]:
            raise RuntimeError("shared wall clock unavailable")
        return NOW

    if runner_succeeds:

        class Runner(_Runner):
            def __call__(
                self,
                request: Any,
                *,
                progress_sink: Any = None,
            ) -> HostSequentialRunResult:
                result = super().__call__(request, progress_sink=progress_sink)
                clock_state["failing"] = True
                return result

    else:

        class Runner(_ThrowsAfterProgressRunner):
            def __call__(
                self,
                request: Any,
                *,
                progress_sink: Any = None,
            ) -> HostSequentialRunResult:
                try:
                    return super().__call__(request, progress_sink=progress_sink)
                finally:
                    clock_state["failing"] = True

    runner = Runner()
    service = ForgeHostService(
        identity=Identity(
            session_id="ses_abcdefgh",
            generation_id="gen_abcdefgh",
            run_id=None,
            revision=0,
        ),
        planner=_Planner(admission),
        sequential_runner=runner,
        current_provider_binding=lambda: admission.provider_binding,
        clock=shared_wall_clock,
        host_run_id_factory=lambda: "run_abcdefgh",
    )
    plan_outcome = service.handle_request(_plan_request())
    service.handle_request(_approve_request(plan_outcome, service.identity))
    request = _run_request(admission, service.identity)

    outcome = service.handle_request(request)
    replay = service.handle_request(request)

    assert outcome.events[-1].payload.type == "run.terminal"
    assert outcome.events[-1].payload.result.status == (
        "succeeded" if runner_succeeds else "failed"
    )
    assert replay.replayed is True
    assert replay.event_bytes == outcome.event_bytes
    assert runner.calls == 1


@pytest.mark.parametrize("runner_succeeds", [True, False])
def test_post_commit_monotonic_failure_uses_commit_proof_or_fails_closed(
    tmp_path: Path,
    runner_succeeds: bool,
) -> None:
    _root, admission = _admission(tmp_path)
    monotonic = _SwitchableMonotonic()
    runner = (
        _BreaksClockAfterSuccessRunner(monotonic)
        if runner_succeeds
        else _BreaksClockAfterProgressRunner(monotonic)
    )
    service = ForgeHostService(
        identity=Identity(
            session_id="ses_abcdefgh",
            generation_id="gen_abcdefgh",
            run_id=None,
            revision=0,
        ),
        planner=_Planner(admission),
        sequential_runner=runner,
        current_provider_binding=lambda: admission.provider_binding,
        clock=lambda: NOW,
        monotonic=monotonic,
        host_run_id_factory=lambda: "run_abcdefgh",
    )
    plan_outcome = service.handle_request(_plan_request())
    service.handle_request(_approve_request(plan_outcome, service.identity))
    request = _run_request(admission, service.identity)

    outcome = service.handle_request(request)
    replay = service.handle_request(request)

    terminal = outcome.events[-1].payload.result
    if runner_succeeds:
        assert terminal.status == "succeeded"
        assert terminal.timing.timed_out is False
        assert terminal.timing.elapsed_ms == 10
    else:
        assert terminal.status == "failed"
        assert terminal.timing.timed_out is True
        assert terminal.timing.elapsed_ms == admission.plan_ready.execution_admission.run_timeout_ms
    assert replay.replayed is True
    assert replay.event_bytes == outcome.event_bytes
    assert runner.calls == 1
    assert service.phase == "terminal"


def _approved_service_with_prepared_runner(
    tmp_path: Path,
    runner: _PreparedRunner,
    *,
    service_type: type[ForgeHostService] = ForgeHostService,
) -> tuple[ForgeHostService, Any, RequestEnvelope]:
    _root, admission = _admission(tmp_path)
    service = service_type(
        identity=Identity(
            session_id="ses_abcdefgh",
            generation_id="gen_abcdefgh",
            run_id=None,
            revision=0,
        ),
        planner=_Planner(admission),
        sequential_runner=runner,
        current_provider_binding=lambda: admission.provider_binding,
        clock=lambda: NOW,
        host_run_id_factory=lambda: "run_abcdefgh",
    )
    plan_outcome = service.handle_request(_plan_request())
    service.handle_request(_approve_request(plan_outcome, service.identity))
    return service, admission, _run_request(admission, service.identity)


class _FailsPreparedPublicationOnce(ForgeHostService):
    def __init__(self, *args: Any, failure_boundary: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.failure_boundary = failure_boundary
        self.failed_once = False

    def _prepare_run_publication(self, *, request: Any, events: Any) -> Any:
        if self.failure_boundary == "prepare" and not self.failed_once:
            self.failed_once = True
            raise RuntimeError("staged publication allocation failed")
        return super()._prepare_run_publication(request=request, events=events)

    def _publish_run_publication(self, prepared: Any) -> None:
        super()._publish_run_publication(prepared)
        if self.failure_boundary == "publish" and not self.failed_once:
            self.failed_once = True
            raise RuntimeError("staged publication assignment failed")


def test_prepared_integration_seals_only_with_authoritative_publication_and_replays(
    tmp_path: Path,
) -> None:
    runner = _PreparedRunner()
    service, admission, request = _approved_service_with_prepared_runner(tmp_path, runner)

    outcome = service.handle_request(request)
    replay = service.handle_request(request)

    transaction = runner.transaction
    assert transaction is not None
    assert transaction.calls == ["commit", "finalize", "complete"]
    assert transaction.state == "SEALED"
    assert transaction.guard_held is False
    assert transaction.publication_binding == (
        service._publication_digest(outcome.event_bytes),  # noqa: SLF001
        sum(len(value) for value in outcome.event_bytes),
    )
    assert (
        _git(
            admission.workspace_git.git_root,
            "rev-parse",
            f"refs/heads/{admission.workspace_git.base_branch}",
        )
        == transaction.facts.prepared_merge_commit
    )
    assert outcome.events[-1].payload.result.status == "succeeded"
    assert outcome.events[-1].payload.result.timing.elapsed_ms == 20
    assert replay.replayed is True
    assert replay.event_bytes == outcome.event_bytes
    assert runner.calls == 1


@pytest.mark.parametrize("fail_at", ["commit", "finalize", "complete"])
def test_prepared_integration_failure_rolls_back_before_failed_publication(
    tmp_path: Path,
    fail_at: str,
) -> None:
    runner = _PreparedRunner(fail_at=fail_at)
    service, admission, request = _approved_service_with_prepared_runner(tmp_path, runner)

    outcome = service.handle_request(request)
    replay = service.handle_request(request)

    transaction = runner.transaction
    assert transaction is not None
    assert transaction.state == "ROLLED_BACK"
    assert transaction.guard_held is False
    assert transaction.calls[-1] == "rollback"
    assert (
        _git(
            admission.workspace_git.git_root,
            "rev-parse",
            f"refs/heads/{admission.workspace_git.base_branch}",
        )
        == admission.workspace_git.base_head
    )
    assert outcome.events[-1].payload.result.status != "succeeded"
    assert replay.replayed is True
    assert replay.event_bytes == outcome.event_bytes
    assert runner.calls == 1


def test_prepared_integration_rollback_failure_requires_manual_attention(
    tmp_path: Path,
) -> None:
    runner = _PreparedRunner(fail_at="rollback", bad_digest=True)
    service, _admission, request = _approved_service_with_prepared_runner(tmp_path, runner)

    outcome = service.handle_request(request)

    transaction = runner.transaction
    assert transaction is not None
    assert transaction.state == "ROLLBACK_FAILED"
    terminal = outcome.events[-1].payload.result
    assert terminal.status == "interrupted"
    assert terminal.reason.code == "integration_rollback_failed"
    assert terminal.reason.retryable is False
    assert terminal.exit_code == 2


def test_sealed_guard_release_is_retried_without_reclassifying_success(
    tmp_path: Path,
) -> None:
    runner = _PreparedRunner(fail_at="guard_release")
    service, _admission, request = _approved_service_with_prepared_runner(tmp_path, runner)

    outcome = service.handle_request(request)

    transaction = runner.transaction
    assert transaction is not None
    assert outcome.events[-1].payload.result.status == "succeeded"
    assert transaction.state == "SEALED"
    assert transaction.guard_held is True
    assert service.pending_guard_release_count == 1

    replay = service.handle_request(request)

    assert replay.replayed is True
    assert replay.event_bytes == outcome.event_bytes
    assert transaction.guard_held is False
    assert service.pending_guard_release_count == 0
    assert transaction.guard_retry_calls == 2


def test_post_seal_diagnostic_mismatch_cannot_reclassify_integrated_success(
    tmp_path: Path,
) -> None:
    runner = _PreparedRunner(fail_at="bad_return")
    service, admission, request = _approved_service_with_prepared_runner(tmp_path, runner)

    outcome = service.handle_request(request)
    replay = service.handle_request(request)

    transaction = runner.transaction
    assert transaction is not None
    assert transaction.state == "SEALED"
    assert (
        _git(
            admission.workspace_git.git_root,
            "rev-parse",
            f"refs/heads/{admission.workspace_git.base_branch}",
        )
        == transaction.facts.prepared_merge_commit
    )
    assert outcome.events[-1].payload.result.status == "succeeded"
    assert replay.replayed is True
    assert replay.event_bytes == outcome.event_bytes


@pytest.mark.parametrize("failure_boundary", ["prepare", "publish"])
def test_prepared_publication_failure_restores_service_image_and_rolls_back(
    tmp_path: Path,
    failure_boundary: str,
) -> None:
    runner = _PreparedRunner()

    class Service(_FailsPreparedPublicationOnce):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, failure_boundary=failure_boundary, **kwargs)

    service, admission, request = _approved_service_with_prepared_runner(
        tmp_path,
        runner,
        service_type=Service,
    )

    outcome = service.handle_request(request)
    replay = service.handle_request(request)

    transaction = runner.transaction
    assert transaction is not None
    assert transaction.state == "ROLLED_BACK"
    assert transaction.calls[-1] == "rollback"
    assert (
        _git(
            admission.workspace_git.git_root,
            "rev-parse",
            f"refs/heads/{admission.workspace_git.base_branch}",
        )
        == admission.workspace_git.base_head
    )
    assert outcome.events[-1].payload.result.status != "succeeded"
    assert all(
        not (event.payload.type == "run.terminal" and event.payload.result.status == "succeeded")
        for event in outcome.events
    )
    assert replay.replayed is True
    assert replay.event_bytes == outcome.event_bytes


def test_service_retains_valid_progress_prefix_when_later_conversion_raises(
    tmp_path: Path,
) -> None:
    _root, admission = _admission(tmp_path)
    runner = _ThrowsAfterMalformedProgressRunner()
    service = ForgeHostService(
        identity=Identity(
            session_id="ses_abcdefgh",
            generation_id="gen_abcdefgh",
            run_id=None,
            revision=0,
        ),
        planner=_Planner(admission),
        sequential_runner=runner,
        current_provider_binding=lambda: admission.provider_binding,
        clock=lambda: NOW,
        host_run_id_factory=lambda: "run_abcdefgh",
    )
    plan_outcome = service.handle_request(_plan_request())
    service.handle_request(_approve_request(plan_outcome, service.identity))
    request = _run_request(admission, service.identity)

    outcome = service.handle_request(request)
    replay = service.handle_request(request)

    assert [event.payload.type for event in outcome.events] == [
        "run.progress",
        "evidence.ready",
        "run.terminal",
    ]
    terminal = outcome.events[-1].payload.result
    assert terminal.status == "failed"
    assert terminal.task_usage[0].model_steps == 1
    assert terminal.workers.started == 1
    assert terminal.workers.failed == 1
    assert replay.replayed is True
    assert replay.event_bytes == outcome.event_bytes
    assert runner.calls == 1
