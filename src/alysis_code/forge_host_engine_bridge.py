"""Bridge Forge Host Protocol v2 to the canonical sequential Forge engine.

The Host service owns protocol identity, approval, replay, and the shared run
deadline.  The sequential engine owns guarded scheduling and native execution.
This module is the only translation boundary between those two domains; it
does not contain planner, scheduler, verification, or terminal policy of its
own.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from .forge_host_protocol.models import (
    ArtifactRecord,
    CheckResult,
    EvidenceReady,
    EvidenceRecord,
    TaskExecutionUsage,
    TerminalReason,
    TerminalResult,
    Timing,
    Usage,
    VerificationResult,
    Workers,
)
from .forge_host_service import (
    HostIntegrationCommitProof,
    HostIntegrationFinalizeProof,
    HostSequentialProgress,
    HostSequentialRunRequest,
    HostSequentialRunResult,
)
from .forge_host_verification import HostVerificationBinding, HostVerificationResult
from .forge_sequential_engine import (
    IntegrationCommitProof,
    IntegrationFinalizeProof,
    Phase0ExecutionControls,
    PreparedIntegration,
    PreparedIntegrationFacts,
    SequentialRunDependencies,
    SequentialRunOutcome,
    SequentialRunProgress,
    SequentialRunRequest,
    TaskExecutionOutcome,
    run_sequential_forge,
)


class ForgeHostEngineBridgeError(RuntimeError):
    """The native engine could not be represented truthfully on Host v2."""


def _host_commit_proof(proof: IntegrationCommitProof) -> HostIntegrationCommitProof:
    return HostIntegrationCommitProof(
        base_branch=proof.base_branch,
        approved_base_head=proof.approved_base_head,
        task_commit=proof.task_commit,
        merge_commit=proof.merge_commit,
        integration_committed_elapsed_ms=proof.integration_committed_elapsed_ms,
    )


def _host_finalize_proof(proof: IntegrationFinalizeProof) -> HostIntegrationFinalizeProof:
    return HostIntegrationFinalizeProof(
        commit_proof=_host_commit_proof(proof.commit_proof),
        final_elapsed_ms=proof.final_elapsed_ms,
    )


class _HostPreparedIntegration:
    """Import-neutral Host view over the engine-owned transaction object."""

    def __init__(self, native: PreparedIntegration) -> None:
        self._native = native
        self._finalize_proof: HostIntegrationFinalizeProof | None = None

    @property
    def native(self) -> PreparedIntegration:
        return self._native

    @property
    def facts(self) -> PreparedIntegrationFacts:
        return self._native.facts

    @property
    def state(self) -> str:
        return self._native.state

    @property
    def guard_release_pending(self) -> bool:
        return self._native.guard_release_pending

    def commit_reversible(self) -> HostIntegrationCommitProof:
        return _host_commit_proof(self._native.commit_reversible())

    def rollback(self, reason: str) -> None:
        self._native.rollback(reason)

    def abort(self, reason: str) -> None:
        self._native.abort(reason)

    def finalize_reversible(self) -> HostIntegrationFinalizeProof:
        proof = _host_finalize_proof(self._native.finalize_reversible())
        self._finalize_proof = proof
        return proof

    def complete_publish(
        self,
        *,
        event_bytes_digest: str,
        event_bytes: int,
    ) -> HostIntegrationFinalizeProof:
        if self._finalize_proof is None:
            raise ForgeHostEngineBridgeError(
                "integration publication was attempted before reversible finalization"
            )
        self._native.complete_publish(
            event_bytes_digest=event_bytes_digest,
            event_bytes=event_bytes,
        )
        if self._native.state != "SEALED":
            raise ForgeHostEngineBridgeError(
                "integration publication returned before durable sealing"
            )
        return self._finalize_proof

    def retry_guard_release(self) -> bool:
        return self._native.retry_guard_release()


HostDependencyFactory = Callable[
    [HostSequentialRunRequest, tuple[HostVerificationBinding, ...]],
    SequentialRunDependencies,
]


def _opaque_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _worker_id(task_id: str) -> str:
    return _opaque_id("worker", task_id)


def _verification_bindings(
    request: HostSequentialRunRequest,
) -> tuple[HostVerificationBinding, ...]:
    commands = request.plan_ready.verification_contract.commands
    bindings = tuple(
        HostVerificationBinding(command_id=command.command_id, argv=tuple(command.argv))
        for command in commands
        if command.validation_status == "VALID"
    )
    expected = {
        command_id
        for check in request.plan_ready.verification_contract.checks
        if check.domain == "verification"
        for command_id in check.command_ids
    }
    if not bindings or {item.command_id for item in bindings} != expected:
        raise ForgeHostEngineBridgeError(
            "the approved verification contract is not exactly executable"
        )
    return bindings


def _host_progress(progress: SequentialRunProgress) -> HostSequentialProgress:
    if progress.kind == "run_started":
        return HostSequentialProgress(
            phase="preflight",
            summary="The admitted Forge run passed guarded preflight.",
            step=0,
            total_steps=1,
        )
    if progress.kind == "task_started":
        if progress.task_id is None:
            raise ForgeHostEngineBridgeError("task progress omitted its task identity")
        return HostSequentialProgress(
            phase="executing",
            summary="The admitted Forge task is executing.",
            task_id=progress.task_id,
            step=0,
            total_steps=1,
            task_model_steps=0,
            worker_id=_worker_id(progress.task_id),
        )
    if progress.kind == "task_finished":
        if progress.task_id is None or progress.outcome is None:
            raise ForgeHostEngineBridgeError("task completion omitted native execution facts")
        return HostSequentialProgress(
            phase="verifying",
            summary="The admitted Forge task reached its verification boundary.",
            task_id=progress.task_id,
            step=1,
            total_steps=1,
            task_model_steps=progress.outcome.usage.model_calls,
            worker_id=_worker_id(progress.task_id),
        )
    return HostSequentialProgress(
        phase="finalizing",
        summary="The admitted Forge run reached terminal accounting.",
        step=1,
        total_steps=1,
    )


def _relative_artifact(path: Path, workspace_root: Path) -> str:
    try:
        resolved_root = workspace_root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        relative = resolved.relative_to(resolved_root).as_posix()
    except (OSError, ValueError) as exc:
        raise ForgeHostEngineBridgeError(
            "native evidence is not retained beneath the admitted workspace"
        ) from exc
    if not relative:
        raise ForgeHostEngineBridgeError("native evidence cannot identify the workspace root")
    return relative


def _artifact_manifest(
    outcomes: Sequence[TaskExecutionOutcome],
    workspace_root: Path,
) -> tuple[tuple[ArtifactRecord, ...], dict[tuple[str, str, int], str], dict[str, str]]:
    artifacts: list[ArtifactRecord] = []
    by_binding: dict[tuple[str, str, int], str] = {}
    by_path: dict[str, str] = {}
    for outcome in outcomes:
        report_path = outcome.report_path.resolve(strict=False)
        patch_path = outcome.patch_path.resolve(strict=False)
        verification_paths = {
            item.artifact.path.resolve(strict=False) for item in outcome.verification
        }
        log_paths = {item.resolve(strict=False) for item in outcome.retained_log_paths}
        for retained in outcome.artifacts:
            relative = _relative_artifact(retained.path, workspace_root)
            key = (relative, retained.digest, retained.size_bytes)
            if key in by_binding:
                continue
            artifact_id = _opaque_id(
                "artifact",
                outcome.task_id,
                relative,
                retained.digest,
                str(retained.size_bytes),
            )
            resolved = retained.path.resolve(strict=False)
            if resolved == report_path:
                role, kind = "report", "report"
            elif resolved == patch_path:
                role, kind = "evidence", "diff"
            elif resolved in log_paths:
                role, kind = "log", "log"
            elif resolved in verification_paths:
                role, kind = "evidence", "log"
            else:
                role, kind = "evidence", "file"
            artifacts.append(
                ArtifactRecord(
                    artifact_id=artifact_id,
                    role=role,
                    kind=kind,
                    status="created",
                    workspace_relative_path=relative,
                    digest=retained.digest,
                    size_bytes=retained.size_bytes,
                )
            )
            by_binding[key] = artifact_id
            by_path[relative] = artifact_id
    return tuple(artifacts), by_binding, by_path


def _result_by_command(
    outcomes: Sequence[TaskExecutionOutcome],
) -> dict[str, HostVerificationResult]:
    results: dict[str, HostVerificationResult] = {}
    for outcome in outcomes:
        for result in outcome.verification:
            if result.command_id in results:
                raise ForgeHostEngineBridgeError(
                    "native verification returned a duplicate command identity"
                )
            results[result.command_id] = result
    return results


def _artifact_for_verification(
    result: HostVerificationResult,
    workspace_root: Path,
    artifact_ids: dict[tuple[str, str, int], str],
) -> str:
    relative = _relative_artifact(result.artifact.path, workspace_root)
    try:
        return artifact_ids[(relative, result.artifact.digest, result.artifact.size_bytes)]
    except KeyError as exc:
        raise ForgeHostEngineBridgeError(
            "verification evidence is absent from the immutable artifact manifest"
        ) from exc


def _aggregate_status(checks: Sequence[CheckResult]) -> str:
    statuses = {item.status for item in checks}
    if checks and statuses == {"passed"}:
        return "passed"
    if "failed" in statuses:
        return "failed"
    return "not_run"


def _evidence_and_checks(
    *,
    request: HostSequentialRunRequest,
    outcome: SequentialRunOutcome,
) -> tuple[EvidenceReady, VerificationResult, VerificationResult]:
    artifacts, artifact_ids, artifacts_by_path = _artifact_manifest(
        outcome.task_outcomes,
        request.native_paths.root,
    )
    command_results = _result_by_command(outcome.task_outcomes)
    task_outcomes = {item.task_id: item for item in outcome.task_outcomes}
    evidence: list[EvidenceRecord] = []
    acceptance_checks: list[CheckResult] = []
    verification_checks: list[CheckResult] = []

    for definition in request.plan_ready.verification_contract.checks:
        evidence_ids: list[str] = []
        check_status = "not_run"
        if definition.domain == "acceptance":
            task = task_outcomes.get(definition.task_id or "")
            if (
                (outcome.success or outcome.completion_ready)
                and task is not None
                and task.completion_consistent
            ):
                report_relative = _relative_artifact(task.report_path, request.native_paths.root)
                artifact_id = artifacts_by_path.get(report_relative)
                if artifact_id is None:
                    raise ForgeHostEngineBridgeError(
                        "accepted native completion lacks its retained report artifact"
                    )
                evidence_id = _opaque_id("evidence", definition.check_id, "acceptance")
                evidence.append(
                    EvidenceRecord(
                        evidence_id=evidence_id,
                        role="acceptance",
                        kind="review",
                        status="passed",
                        check_id=definition.check_id,
                        task_id=definition.task_id,
                        requirement_ids=list(definition.requirement_ids),
                        command_ids=[],
                        real_execution=None,
                        exit_code=None,
                        timed_out=None,
                        summary="The admitted task completed with exact scope and Git evidence.",
                        artifact_refs=[artifact_id],
                    )
                )
                evidence_ids.append(evidence_id)
                check_status = "passed"
            else:
                evidence_id = _opaque_id("evidence", definition.check_id, "not_run")
                evidence.append(
                    EvidenceRecord(
                        evidence_id=evidence_id,
                        role="acceptance",
                        kind="review",
                        status="not_run",
                        check_id=definition.check_id,
                        task_id=definition.task_id,
                        requirement_ids=list(definition.requirement_ids),
                        command_ids=[],
                        real_execution=None,
                        exit_code=None,
                        timed_out=None,
                        summary="The admitted acceptance check did not complete.",
                        artifact_refs=[],
                    )
                )
                evidence_ids.append(evidence_id)
            acceptance_checks.append(
                CheckResult(
                    check_id=definition.check_id,
                    task_id=definition.task_id,
                    requirement_ids=list(definition.requirement_ids),
                    status=check_status,
                    summary=(
                        "The admitted acceptance check passed."
                        if check_status == "passed"
                        else "The admitted acceptance check was not completed."
                    ),
                    evidence_refs=evidence_ids,
                )
            )
            continue

        result_statuses: list[str] = []
        for command_id in definition.command_ids:
            result = command_results.get(command_id)
            if result is None:
                evidence_id = _opaque_id("evidence", definition.check_id, command_id, "not_run")
                evidence.append(
                    EvidenceRecord(
                        evidence_id=evidence_id,
                        role="verification",
                        kind="test",
                        status="not_run",
                        check_id=definition.check_id,
                        task_id=definition.task_id,
                        requirement_ids=list(definition.requirement_ids),
                        command_ids=[command_id],
                        real_execution=False,
                        exit_code=None,
                        timed_out=False,
                        summary="The admitted verification command did not run.",
                        artifact_refs=[],
                    )
                )
                result_statuses.append("not_run")
            else:
                artifact_id = _artifact_for_verification(
                    result,
                    request.native_paths.root,
                    artifact_ids,
                )
                evidence_id = _opaque_id("evidence", definition.check_id, command_id)
                status = "passed" if result.passed else "failed"
                evidence.append(
                    EvidenceRecord(
                        evidence_id=evidence_id,
                        role="verification",
                        kind="test",
                        status=status,
                        check_id=definition.check_id,
                        task_id=definition.task_id,
                        requirement_ids=list(definition.requirement_ids),
                        command_ids=[command_id],
                        real_execution=result.real_execution,
                        exit_code=result.exit_code,
                        timed_out=result.timed_out,
                        summary=(
                            "The admitted verification command passed."
                            if result.passed
                            else "The admitted verification command failed."
                        ),
                        artifact_refs=[artifact_id],
                    )
                )
                result_statuses.append(status)
            evidence_ids.append(evidence_id)
        check_status = (
            "passed"
            if result_statuses and set(result_statuses) == {"passed"}
            else "failed"
            if "failed" in result_statuses
            else "not_run"
        )
        verification_checks.append(
            CheckResult(
                check_id=definition.check_id,
                task_id=definition.task_id,
                requirement_ids=list(definition.requirement_ids),
                status=check_status,
                summary=(
                    "The admitted verification check passed."
                    if check_status == "passed"
                    else "The admitted verification check did not pass."
                ),
                evidence_refs=evidence_ids,
            )
        )

    return (
        EvidenceReady(type="evidence.ready", evidence=evidence, artifacts=list(artifacts)),
        VerificationResult(
            status=_aggregate_status(acceptance_checks),
            checks=acceptance_checks,
        ),
        VerificationResult(
            status=_aggregate_status(verification_checks),
            checks=verification_checks,
        ),
    )


def _terminal(
    *,
    request: HostSequentialRunRequest,
    outcome: SequentialRunOutcome,
    evidence: EvidenceReady,
    acceptance: VerificationResult,
    verification: VerificationResult,
    progress: Sequence[HostSequentialProgress],
) -> TerminalResult:
    success = bool(
        (outcome.success or outcome.completion_ready)
        and outcome.completion_consistent
        and (
            outcome.success
            or (
                outcome.prepared_integration is not None
                and outcome.prepared_integration.state == "PREPARED"
            )
        )
    )
    if success:
        status = "succeeded"
        code = "completed_successfully"
        message = "The admitted Forge run completed successfully."
        retryable = False
        exit_code = 0
    elif outcome.timed_out or outcome.stopped_reason == "deadline_exhausted":
        status = "interrupted"
        code = "run_timed_out"
        message = "The admitted Forge run exhausted its deadline."
        retryable = True
        exit_code = 2
    elif outcome.stopped_reason in {"engine_error", "task_error"}:
        status = "interrupted"
        code = "engine_interrupted"
        message = "The native Forge engine was interrupted."
        retryable = True
        exit_code = 2
    else:
        status = "failed"
        code = "task_failed"
        message = "The admitted Forge task did not satisfy its contract."
        retryable = False
        exit_code = 1

    task_steps = {item.task_id: item.usage.model_calls for item in outcome.task_outcomes}
    task_ids = [task.task_id for task in request.plan_ready.plan_definition.tasks]
    started = bool(outcome.task_outcomes or any(item.worker_id is not None for item in progress))
    usage = outcome.usage
    return TerminalResult(
        host_plan_fingerprint=request.plan_ready.host_plan_fingerprint,
        native_approval_fingerprint=request.plan_ready.native_approval_fingerprint,
        verification_contract_fingerprint=(
            request.plan_ready.verification_contract.contract_fingerprint
        ),
        execution_admission_fingerprint=request.plan_ready.execution_admission.fingerprint,
        status=status,
        reason=TerminalReason(code=code, message=message, retryable=retryable),
        exit_code=exit_code,
        acceptance=acceptance,
        verification=verification,
        usage=Usage(
            model_calls=usage.model_calls,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_known=usage.cost_known,
            total_cost_micros=usage.total_cost_micros,
        ),
        timing=Timing(
            started_at=datetime.fromisoformat(outcome.started_at),
            finished_at=datetime.fromisoformat(outcome.finished_at),
            elapsed_ms=outcome.elapsed_ms,
            timed_out=outcome.timed_out,
        ),
        workers=Workers(
            requested=1,
            started=1 if started else 0,
            succeeded=1 if success else 0,
            failed=1 if started and not success else 0,
        ),
        task_usage=[
            TaskExecutionUsage(task_id=task_id, model_steps=task_steps.get(task_id, 0))
            for task_id in task_ids
        ],
        artifact_refs=[artifact.artifact_id for artifact in evidence.artifacts],
    )


class ForgeHostEngineRunner:
    """Production Host runner over the canonical guarded sequential engine."""

    def __init__(self, *, dependency_factory: HostDependencyFactory) -> None:
        self._dependency_factory = dependency_factory

    def __call__(
        self,
        request: HostSequentialRunRequest,
        *,
        progress_sink: Callable[[HostSequentialProgress], None] | None = None,
    ) -> HostSequentialRunResult:
        bindings = _verification_bindings(request)
        dependencies = self._dependency_factory(request, bindings)
        registered_native: PreparedIntegration | None = None
        registered_host: _HostPreparedIntegration | None = None

        def _register_prepared(native: PreparedIntegration) -> None:
            nonlocal registered_native, registered_host
            if registered_native is not None or registered_host is not None:
                raise ForgeHostEngineBridgeError(
                    "the native engine registered more than one integration"
                )
            host_view = _HostPreparedIntegration(native)
            if request.register_prepared_integration is None:
                raise ForgeHostEngineBridgeError(
                    "the Host service did not provide transaction registration"
                )
            request.register_prepared_integration(host_view)
            registered_native = native
            registered_host = host_view

        # The Host callback performs exact admission/provider/Git/pytest
        # revalidation.  Replacing the native reconciler guarantees that this
        # one-shot callback is first invoked only after the sequential engine
        # has acquired its mutation guard.
        dependencies = replace(
            dependencies,
            reconcile_and_validate=lambda _paths, _plan: request.reload_native_plan_under_guard(),
        )
        translated_progress: list[HostSequentialProgress] = []

        def _observe(progress: SequentialRunProgress) -> None:
            translated = _host_progress(progress)
            translated_progress.append(translated)
            if progress_sink is not None:
                progress_sink(translated)

        outcome = run_sequential_forge(
            SequentialRunRequest(
                native_run_id=request.native_run_id,
                native_paths=request.native_paths,
                retry_failed=False,
                retry_changes_requested=False,
                max_attempts=1,
                only_ids=frozenset(
                    task.task_id for task in request.plan_ready.plan_definition.tasks
                ),
                max_tasks=1,
                keep_going=False,
                git_flow=True,
                verify_mode="strict",
                scope_mode="strict",
                base_branch=request.base_branch,
                base_head=request.base_head,
                started_at=request.started_at.isoformat(),
                execution_deadline=request.execution_deadline,
                dry_run=False,
                phase0=True,
                phase0_controls=Phase0ExecutionControls(),
                required_verification_bindings=bindings,
            ),
            dependencies=dependencies,
            observer=_observe,
            prepared_integration_sink=_register_prepared,
        )
        if outcome.completion_ready:
            if (
                outcome.prepared_integration is None
                or registered_native is None
                or registered_host is None
                or outcome.prepared_integration is not registered_native
            ):
                raise ForgeHostEngineBridgeError(
                    "the native engine replaced its registered integration authority"
                )
        elif outcome.prepared_integration is not None or registered_host is not None:
            if registered_host is not None and registered_host.state == "PREPARED":
                registered_host.abort("native engine did not return a committable outcome")
            raise ForgeHostEngineBridgeError(
                "a failed native outcome retained integration authority"
            )
        evidence, acceptance, verification = _evidence_and_checks(
            request=request,
            outcome=outcome,
        )
        terminal = _terminal(
            request=request,
            outcome=outcome,
            evidence=evidence,
            acceptance=acceptance,
            verification=verification,
            progress=translated_progress,
        )
        return HostSequentialRunResult(
            progress=tuple(translated_progress),
            evidence=(evidence,),
            terminal=terminal,
            prepared_integration=(registered_host if outcome.completion_ready else None),
            integration_commit_proof=None,
        )


__all__ = [
    "ForgeHostEngineBridgeError",
    "ForgeHostEngineRunner",
    "HostDependencyFactory",
]
