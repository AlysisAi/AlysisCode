"""Stateful Forge Host Protocol v2 invariants.

JSON Schema and Pydantic validate one message.  This ledger validates authority
across a stream: optimistic revisions, replay, approval issuance/consumption,
immutable evidence, plan/contract admission, and a single final terminal event.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from .canonical import (
    approval_action_fingerprint,
    approval_policy_fingerprint,
    approval_scope_fingerprint,
    canonical_fingerprint,
    execution_admission_fingerprint,
    plan_fingerprint,
    verification_contract_fingerprint,
)
from .codec import encode_envelope, semantic_payload_digest, validate_envelope
from .errors import (
    ApprovalStateError,
    EventSequenceError,
    IdempotencyConflictError,
    PlanStateError,
    ReferenceResolutionError,
    RevisionGapError,
    StaleIdentityError,
    StaleRevisionError,
    TerminalEventError,
)
from .models import (
    ApprovalDecision,
    ApprovalRequired,
    EventEnvelope,
    EvidenceReady,
    EvidenceRecord,
    HandshakeRequest,
    Identity,
    PlanExecuteAction,
    PlannerAnswer,
    PlannerInput,
    PlannerQuestion,
    PlanReady,
    RequestEnvelope,
    RunProgress,
    RunStart,
    RunTerminal,
    StateSnapshot,
    TerminalResult,
    VerificationCheckDefinition,
)


class RequestDisposition(StrEnum):
    ACCEPTED = "accepted"
    REPLAY = "replay"


@dataclass(frozen=True)
class PendingApproval:
    operation_id: str
    authority: tuple[str, str, str | None]
    issued_revision: int
    approval_id: str
    action_fingerprint: str
    scope_fingerprint: str
    expires_at: datetime
    action_kind: str
    decision: str | None = None


class ProtocolLedger:
    """Validate one authoritative session/generation/run event stream."""

    def __init__(
        self,
        identity: Identity,
        *,
        after_sequence: int = 0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if after_sequence != 0:
            raise EventSequenceError("replay resume requires an authenticated durable checkpoint")
        if identity.run_id is not None:
            raise StaleIdentityError(
                "a Phase-0 ledger must begin with pre-run identity and allocate runId via bind_run"
            )
        self._identity = Identity.model_validate_json(
            identity.model_dump_json(by_alias=True), strict=True
        )
        self._last_sequence = 0
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._requests: dict[str, str] = {}
        self._request_operations: dict[str, str] = {}
        self._evidence: dict[str, EvidenceRecord] = {}
        self._artifacts: dict[str, object] = {}
        self._pending_approvals: dict[str, PendingApproval] = {}
        self._consumed_approval_ids: set[str] = set()
        self._plan: PlanReady | None = None
        self._plan_start_authorized = False
        self._plan_start_authorization_expires_at: datetime | None = None
        self._run_start_consumed = False
        self._run_bound = False
        self._run_operation_id: str | None = None
        self._current_planner_operation: str | None = None
        self._pending_planner_questions: dict[str, str] = {}
        self._consumed_planner_question_ids: set[str] = set()
        self._contract_checks: dict[str, VerificationCheckDefinition] = {}
        self._plan_task_ids: set[str] = set()
        self._task_model_steps: dict[str, int] = {}
        self._terminal_seen = False

    @staticmethod
    def _private_request(envelope: RequestEnvelope) -> RequestEnvelope:
        private = validate_envelope(encode_envelope(envelope))
        if not isinstance(private, RequestEnvelope):
            raise TypeError("request isolation produced the wrong envelope kind")
        return private

    @staticmethod
    def _private_event(envelope: EventEnvelope) -> EventEnvelope:
        private = validate_envelope(encode_envelope(envelope))
        if not isinstance(private, EventEnvelope):
            raise TypeError("event isolation produced the wrong envelope kind")
        return private

    def _refresh_plan_bindings(self) -> PlanReady:
        if self._plan is None:
            raise PlanStateError("the canonical plan is unavailable")
        try:
            plan = PlanReady.model_validate_json(
                self._plan.model_dump_json(by_alias=True), strict=True
            )
        except Exception as exc:
            raise PlanStateError("stored plan authority failed canonical revalidation") from exc
        if (
            plan_fingerprint(plan.plan_definition) != plan.host_plan_fingerprint
            or canonical_fingerprint(plan.native_approval_definition)
            != plan.native_approval_fingerprint
            or verification_contract_fingerprint(plan.verification_contract)
            != plan.verification_contract.contract_fingerprint
            or execution_admission_fingerprint(plan.execution_admission)
            != plan.execution_admission.fingerprint
            or approval_policy_fingerprint(plan.approval_policy) != plan.approval_policy.fingerprint
            or (
                plan.approval_policy.scope is not None
                and approval_scope_fingerprint(plan.approval_policy.scope)
                != plan.approval_policy.scope.fingerprint
            )
        ):
            raise PlanStateError("stored plan authority fingerprints are inconsistent")
        self._plan = plan
        return plan

    @property
    def identity(self) -> Identity:
        return self._identity

    @property
    def last_sequence(self) -> int:
        return self._last_sequence

    @property
    def terminal_seen(self) -> bool:
        return self._terminal_seen

    @property
    def evidence_ids(self) -> frozenset[str]:
        return frozenset(self._evidence)

    @property
    def artifact_ids(self) -> frozenset[str]:
        return frozenset(self._artifacts)

    @property
    def pending_approval_ids(self) -> frozenset[str]:
        return frozenset(self._pending_approvals)

    @property
    def consumed_approval_ids(self) -> frozenset[str]:
        return frozenset(self._consumed_approval_ids)

    @property
    def host_plan_fingerprint(self) -> str | None:
        return self._plan.host_plan_fingerprint if self._plan is not None else None

    @property
    def plan_fingerprint(self) -> str | None:
        """Compatibility alias for the explicitly versioned Host plan binding."""

        return self.host_plan_fingerprint

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ApprovalStateError("approval clock must return an aware timestamp")
        return value

    def _assert_stable_identity(self, identity: Identity) -> None:
        if identity.stable_key != self._identity.stable_key:
            raise StaleIdentityError(
                "message identity does not match the active session/generation/run"
            )

    def _assert_session_generation(self, identity: Identity) -> None:
        if (
            identity.session_id != self._identity.session_id
            or identity.generation_id != self._identity.generation_id
        ):
            raise StaleIdentityError(
                "message identity does not match the active session/generation"
            )

    def _assert_revision_transition(self, identity: Identity) -> None:
        if identity.revision < self._identity.revision:
            raise StaleRevisionError("event revision is older than the active revision")
        if identity.revision > self._identity.revision + 1:
            raise RevisionGapError("event revision skipped an authoritative revision")

    def bind_run(self, identity: Identity) -> None:
        """Bind a pre-run ledger to the engine-allocated run exactly once."""

        self._refresh_plan_bindings()
        identity = Identity.model_validate_json(
            identity.model_dump_json(by_alias=True), strict=True
        )
        if self._terminal_seen:
            raise TerminalEventError("the terminal event is final")
        if self._run_operation_id is None or not self._run_start_consumed:
            raise PlanStateError("run binding requires an admitted run.start")
        if self._run_bound:
            raise StaleIdentityError("runId allocation can be bound exactly once")
        self._assert_session_generation(identity)
        if identity.run_id is None:
            raise StaleIdentityError("run binding requires runId")
        if self._identity.run_id is not None and identity.run_id != self._identity.run_id:
            raise StaleIdentityError("runId cannot be rebound")
        self._assert_revision_transition(identity)
        self._identity = identity
        self._run_bound = True

    def accept_request(self, envelope: RequestEnvelope) -> RequestDisposition:
        """Accept a semantic command or identify an exact idempotent replay."""

        envelope = self._private_request(envelope)
        if self._terminal_seen:
            raise TerminalEventError("the terminal event is final")
        if isinstance(envelope.payload, PlannerInput) and self._plan is not None:
            raise PlanStateError("planner input is closed after plan.ready")
        digest = semantic_payload_digest(envelope)
        previous = self._requests.get(envelope.idempotency_key)
        if previous is not None:
            if previous != digest:
                raise IdempotencyConflictError(
                    "idempotency key was already used for different request content"
                )
            return RequestDisposition.REPLAY

        if envelope.operation_id is not None:
            operation_digest = self._request_operations.get(envelope.operation_id)
            if operation_digest is not None:
                if operation_digest != digest:
                    raise IdempotencyConflictError(
                        "operationId was already used for a different command"
                    )
                self._requests[envelope.idempotency_key] = digest
                return RequestDisposition.REPLAY

        payload = envelope.payload
        if not isinstance(payload, HandshakeRequest):
            self._assert_stable_identity(payload.identity)
            if payload.expected_revision != self._identity.revision:
                raise StaleRevisionError("command expectedRevision is stale")

        if isinstance(payload, ApprovalDecision):
            self._accept_approval_decision(payload)
        elif isinstance(payload, RunStart):
            self._accept_run_start(envelope, payload)
        elif isinstance(payload, PlannerInput):
            self._accept_planner_input(envelope, payload)

        self._requests[envelope.idempotency_key] = digest
        if envelope.operation_id is not None:
            self._request_operations[envelope.operation_id] = digest
        return RequestDisposition.ACCEPTED

    def _accept_planner_input(
        self,
        envelope: RequestEnvelope,
        payload: PlannerInput,
    ) -> None:
        assert envelope.operation_id is not None
        planner_value = payload.input
        if isinstance(planner_value, PlannerAnswer):
            origin = self._pending_planner_questions.get(planner_value.question_id)
            if origin is None:
                raise PlanStateError(
                    "planner answer must resolve one outstanding required question"
                )
            if origin != self._current_planner_operation:
                raise PlanStateError("planner answer question authority is stale")
            del self._pending_planner_questions[planner_value.question_id]
            self._consumed_planner_question_ids.add(planner_value.question_id)
        elif self._pending_planner_questions:
            raise PlanStateError("an outstanding required planner question must be answered first")
        self._current_planner_operation = envelope.operation_id

    def _accept_approval_decision(self, payload: ApprovalDecision) -> None:
        if payload.approval_id in self._consumed_approval_ids:
            raise ApprovalStateError("approval authority has already been consumed")
        pending = self._pending_approvals.get(payload.approval_id)
        if pending is None:
            raise ApprovalStateError("approval decision has no matching approval.required")
        if pending.action_kind == "plan_execute":
            self._refresh_plan_bindings()
        if payload.approval_operation_id != pending.operation_id:
            raise ApprovalStateError("approval decision operation authority does not match")
        if payload.identity.stable_key != pending.authority:
            raise ApprovalStateError("approval decision identity authority does not match")
        if (
            payload.action_fingerprint != pending.action_fingerprint
            or payload.scope_fingerprint != pending.scope_fingerprint
            or payload.expires_at != pending.expires_at
        ):
            raise ApprovalStateError("approval decision fingerprints do not match")
        if self._now() >= pending.expires_at:
            del self._pending_approvals[payload.approval_id]
            self._consumed_approval_ids.add(payload.approval_id)
            raise ApprovalStateError("approval authority has expired")

        del self._pending_approvals[payload.approval_id]
        self._consumed_approval_ids.add(payload.approval_id)
        if pending.action_kind == "plan_execute":
            self._plan_start_authorized = payload.decision == "allow_once"
            self._plan_start_authorization_expires_at = (
                pending.expires_at if payload.decision == "allow_once" else None
            )

    def _accept_run_start(self, envelope: RequestEnvelope, payload: RunStart) -> None:
        plan = self._refresh_plan_bindings()
        contract = plan.verification_contract
        if (
            payload.host_plan_fingerprint != plan.host_plan_fingerprint
            or payload.native_approval_fingerprint != plan.native_approval_fingerprint
            or payload.verification_contract_fingerprint != contract.contract_fingerprint
            or payload.execution_admission != plan.execution_admission
        ):
            raise PlanStateError(
                "run.start does not match the admitted plan, contract, and execution admission"
            )
        if self._run_start_consumed:
            raise PlanStateError("the one-shot plan execution authority was already consumed")
        if not self._plan_start_authorized:
            raise PlanStateError("the canonical plan has not been approved")
        if (
            self._plan_start_authorization_expires_at is not None
            and self._now() >= self._plan_start_authorization_expires_at
        ):
            self._plan_start_authorized = False
            self._plan_start_authorization_expires_at = None
            raise PlanStateError("the allow-once plan execution authority has expired")
        assert envelope.operation_id is not None
        self._run_start_consumed = True
        self._run_operation_id = envelope.operation_id
        self._plan_start_authorized = False
        self._plan_start_authorization_expires_at = None

    def accept_event(self, envelope: EventEnvelope) -> None:
        """Advance a contiguous event stream after all invariants pass."""

        envelope = self._private_event(envelope)
        if self._terminal_seen:
            raise TerminalEventError("the terminal event is final")
        expected_sequence = self._last_sequence + 1
        if envelope.sequence != expected_sequence:
            raise EventSequenceError("event sequence is not contiguous with the replay cursor")
        self._assert_session_generation(envelope.identity)
        current_run_id = self._identity.run_id
        incoming_run_id = envelope.identity.run_id
        if current_run_id is None and incoming_run_id is not None:
            raise StaleIdentityError("run events require bind_run before delivery")
        if current_run_id is not None and incoming_run_id != current_run_id:
            raise StaleIdentityError("message identity does not match the active run")
        self._assert_revision_transition(envelope.identity)

        payload = envelope.payload
        if isinstance(payload, PlannerQuestion):
            self._accept_planner_question(envelope, payload)
        elif isinstance(payload, PlanReady):
            if envelope.operation_id != self._current_planner_operation:
                raise PlanStateError("plan.ready must match the latest planner input operation")
            if self._pending_planner_questions:
                raise PlanStateError("plan.ready cannot bypass an outstanding required question")
        if isinstance(payload, StateSnapshot):
            raise PlanStateError(
                "state.snapshot is unavailable until its full authority projection is defined"
            )
        if incoming_run_id is not None and self._run_operation_id is not None:
            if envelope.operation_id != self._run_operation_id:
                raise StaleIdentityError("run event operationId does not match admitted run.start")
        if isinstance(payload, PlanReady):
            self._accept_plan_ready(envelope, payload)
        elif isinstance(payload, ApprovalRequired):
            self._accept_approval_required(envelope, payload)
        elif isinstance(payload, RunProgress):
            if payload.task_id is not None and payload.task_id not in self._plan_task_ids:
                raise ReferenceResolutionError("run.progress taskId does not resolve to the plan")
            if payload.task_model_steps is not None:
                plan = self._refresh_plan_bindings()
                if payload.task_model_steps > plan.execution_admission.per_managed_task_max_steps:
                    raise PlanStateError("reported task model steps exceed execution admission")
                assert payload.task_id is not None
                previous_steps = self._task_model_steps.get(payload.task_id)
                if previous_steps is not None and payload.task_model_steps < previous_steps:
                    raise PlanStateError("reported task model steps cannot regress")
                self._task_model_steps[payload.task_id] = payload.task_model_steps
        elif isinstance(payload, EvidenceReady):
            self._accept_evidence(payload)

        terminal_result = (
            payload.result
            if isinstance(payload, RunTerminal)
            else payload.terminal
            if isinstance(payload, StateSnapshot)
            else None
        )
        if terminal_result is not None:
            self._validate_terminal(terminal_result)

        self._last_sequence = envelope.sequence
        self._identity = envelope.identity
        if terminal_result is not None:
            self._terminal_seen = True

    def _accept_planner_question(
        self,
        envelope: EventEnvelope,
        payload: PlannerQuestion,
    ) -> None:
        if self._plan is not None:
            raise PlanStateError("planner questions are closed after plan.ready")
        if envelope.operation_id != self._current_planner_operation:
            raise PlanStateError("planner question must match the latest planner input operation")
        if not payload.required:
            raise PlanStateError("optional planner questions are unavailable in Phase 0")
        if self._pending_planner_questions:
            raise PlanStateError("only one required planner question may be outstanding")
        if (
            payload.question_id in self._consumed_planner_question_ids
            or payload.question_id in self._pending_planner_questions
        ):
            raise PlanStateError("planner questionId cannot be reused")
        assert envelope.operation_id is not None
        self._pending_planner_questions[payload.question_id] = envelope.operation_id

    def _accept_plan_ready(self, envelope: EventEnvelope, payload: PlanReady) -> None:
        if self._plan is not None:
            raise PlanStateError("a generation cannot redefine its canonical plan")
        assert envelope.operation_id is not None
        self._plan = PlanReady.model_validate_json(
            payload.model_dump_json(by_alias=True), strict=True
        )
        payload = self._refresh_plan_bindings()
        self._contract_checks = {
            check.check_id: check for check in payload.verification_contract.checks
        }
        self._plan_task_ids = {task.task_id for task in payload.plan_definition.tasks}
        self._plan_start_authorized = not payload.approval_policy.required
        self._plan_start_authorization_expires_at = None

    def _retire_expired_approvals(self) -> None:
        if not self._pending_approvals:
            # Expiry time is authority only while an approval is pending.  Do
            # not make terminal validation depend on a wall clock after the
            # run's one-shot approval has already been consumed.
            return
        now = self._now()
        expired = [
            approval_id
            for approval_id, pending in self._pending_approvals.items()
            if now >= pending.expires_at
        ]
        for approval_id in expired:
            del self._pending_approvals[approval_id]
            self._consumed_approval_ids.add(approval_id)

    def _accept_approval_required(
        self,
        envelope: EventEnvelope,
        payload: ApprovalRequired,
    ) -> None:
        if (
            payload.approval_id in self._pending_approvals
            or payload.approval_id in self._consumed_approval_ids
        ):
            raise ApprovalStateError("approvalId cannot be reissued or redefined")
        if self._now() >= payload.expires_at:
            raise ApprovalStateError("approval.required is already expired")
        if not isinstance(payload.action, PlanExecuteAction):
            raise ApprovalStateError(
                "runtime approval subjects are unavailable until registered by the engine"
            )
        if isinstance(payload.action, PlanExecuteAction):
            plan = self._refresh_plan_bindings()
            if envelope.identity.run_id is not None:
                raise PlanStateError("plan execution approval must be issued before run allocation")
            if self._run_start_consumed:
                raise PlanStateError("plan execution already started")
            approval = plan.approval_policy
            if not approval.required:
                raise PlanStateError("canonical plan did not require approval")
            if (
                self._plan_start_authorization_expires_at is not None
                and self._now() >= self._plan_start_authorization_expires_at
            ):
                self._plan_start_authorized = False
                self._plan_start_authorization_expires_at = None
            if self._plan_start_authorized:
                raise ApprovalStateError("plan execution already has unused allow-once authority")
            self._retire_expired_approvals()
            if any(
                pending.action_kind == "plan_execute"
                for pending in self._pending_approvals.values()
            ):
                raise ApprovalStateError("an unexpired plan approval issuance is already pending")
            if (
                payload.scope != approval.scope
                or payload.risk != approval.risk
                or payload.summary != approval.summary
                or payload.action.host_plan_fingerprint != plan.host_plan_fingerprint
                or payload.action.native_approval_fingerprint != plan.native_approval_fingerprint
                or payload.action.verification_contract_fingerprint
                != plan.verification_contract.contract_fingerprint
                or payload.action.execution_admission_fingerprint
                != plan.execution_admission.fingerprint
                or payload.action.approval_policy_fingerprint != approval.fingerprint
            ):
                raise PlanStateError(
                    "approval.required does not exactly match canonical plan authority"
                )
            expires_wire = payload.expires_at.isoformat().replace("+00:00", "Z")
            assert envelope.operation_id is not None
            if (
                approval_scope_fingerprint(payload.scope) != payload.scope.fingerprint
                or approval_action_fingerprint(
                    operation_id=envelope.operation_id,
                    session_id=envelope.identity.session_id,
                    generation_id=envelope.identity.generation_id,
                    run_id=envelope.identity.run_id,
                    revision=envelope.identity.revision,
                    approval_id=payload.approval_id,
                    action=payload.action.model_dump(
                        mode="json", by_alias=True, exclude_none=False
                    ),
                    scope_fingerprint=payload.scope.fingerprint,
                    expires_at=expires_wire,
                    risk=payload.risk,
                    summary=payload.summary,
                )
                != payload.action_fingerprint
            ):
                raise ApprovalStateError("approval authority fingerprint is inconsistent")

        assert envelope.operation_id is not None
        self._pending_approvals[payload.approval_id] = PendingApproval(
            operation_id=envelope.operation_id,
            authority=envelope.identity.stable_key,
            issued_revision=envelope.identity.revision,
            approval_id=payload.approval_id,
            action_fingerprint=payload.action_fingerprint,
            scope_fingerprint=payload.scope.fingerprint,
            expires_at=payload.expires_at,
            action_kind=payload.action.kind,
        )

    def _accept_evidence(self, payload: EvidenceReady) -> None:
        incoming_evidence = {record.evidence_id for record in payload.evidence}
        incoming_artifacts = {record.artifact_id for record in payload.artifacts}
        if incoming_evidence.intersection(self._evidence):
            raise ReferenceResolutionError("evidence IDs are immutable across events")
        if incoming_artifacts.intersection(self._artifacts):
            raise ReferenceResolutionError("artifact IDs are immutable across events")
        for record in payload.evidence:
            if record.check_id is None:
                continue
            definition = self._contract_checks.get(record.check_id)
            if definition is None:
                raise ReferenceResolutionError("evidence checkId is not in the approved contract")
            if (
                record.role != definition.domain
                or record.task_id != definition.task_id
                or set(record.requirement_ids) != set(definition.requirement_ids)
                or (
                    record.role == "verification"
                    and record.command_ids[0] not in definition.command_ids
                )
                or (record.role == "acceptance" and record.command_ids)
            ):
                raise ReferenceResolutionError(
                    "evidence authority does not match its approved contract check"
                )
            if record.status == "passed":
                manifest = {artifact.artifact_id: artifact for artifact in payload.artifacts}
                if not record.artifact_refs or any(
                    manifest[artifact_ref].status not in {"created", "updated", "unchanged"}
                    for artifact_ref in record.artifact_refs
                ):
                    raise ReferenceResolutionError(
                        "passed evidence requires available immutable artifact records"
                    )
        self._evidence.update((record.evidence_id, record) for record in payload.evidence)
        self._artifacts.update((record.artifact_id, record) for record in payload.artifacts)

    def _validate_terminal(self, result: TerminalResult) -> None:
        plan = self._refresh_plan_bindings()
        self._retire_expired_approvals()
        if self._pending_approvals:
            raise ApprovalStateError(
                "terminal result cannot strand unexpired pending approval authority"
            )
        contract = plan.verification_contract
        if (
            result.host_plan_fingerprint != plan.host_plan_fingerprint
            or result.native_approval_fingerprint != plan.native_approval_fingerprint
            or result.verification_contract_fingerprint != contract.contract_fingerprint
            or result.execution_admission_fingerprint != plan.execution_admission.fingerprint
        ):
            raise PlanStateError("terminal result fingerprints do not match the admitted run")
        unresolved_artifacts = set(result.artifact_refs).difference(self._artifacts)
        if unresolved_artifacts:
            raise ReferenceResolutionError(
                "terminal artifactRefs must resolve to immutable evidence manifests"
            )
        if result.status == "succeeded" and any(
            getattr(self._artifacts[artifact_id], "status", None)
            not in {"created", "updated", "unchanged"}
            for artifact_id in result.artifact_refs
        ):
            raise ReferenceResolutionError(
                "succeeded terminal can reference only available immutable artifacts"
            )

        for domain, aggregate in (
            ("acceptance", result.acceptance),
            ("verification", result.verification),
        ):
            for check in aggregate.checks:
                definition = self._contract_checks.get(check.check_id)
                if definition is None or definition.domain != domain:
                    raise ReferenceResolutionError(
                        "terminal checkId/domain must resolve to the approved contract"
                    )
                if check.task_id != definition.task_id or set(check.requirement_ids) != set(
                    definition.requirement_ids
                ):
                    raise ReferenceResolutionError(
                        "terminal check authority does not match the approved contract"
                    )
                evidence = [self._evidence.get(reference) for reference in check.evidence_refs]
                if any(record is None for record in evidence):
                    raise ReferenceResolutionError(
                        "terminal evidenceRefs must resolve to prior evidence.ready records"
                    )
                for record in evidence:
                    assert record is not None
                    if (
                        record.role != domain
                        or record.check_id != check.check_id
                        or record.task_id != check.task_id
                        or set(record.requirement_ids) != set(check.requirement_ids)
                    ):
                        raise ReferenceResolutionError(
                            "terminal evidence role and authority must match its check"
                        )
                    if check.status == "passed" and record.status != "passed":
                        raise ReferenceResolutionError(
                            "passed terminal checks require passed matching evidence"
                        )
                evidence_command_ids = [
                    command_id
                    for record in evidence
                    if record is not None
                    for command_id in record.command_ids
                ]
                if len(evidence_command_ids) != len(set(evidence_command_ids)) or set(
                    evidence_command_ids
                ) != set(definition.command_ids):
                    raise ReferenceResolutionError(
                        "terminal evidence must provide one distinct fact for every approved command"
                    )

        expected_task_ids = {task.task_id for task in plan.plan_definition.tasks}
        if {record.task_id for record in result.task_usage} != expected_task_ids:
            raise PlanStateError("terminal task usage must cover every managed plan task")
        usage_by_task = {record.task_id: record for record in result.task_usage}
        if any(
            record.model_steps > plan.execution_admission.per_managed_task_max_steps
            for record in result.task_usage
        ):
            raise PlanStateError("terminal task model steps exceed execution admission")
        if any(
            usage_by_task[task_id].model_steps != model_steps
            for task_id, model_steps in self._task_model_steps.items()
        ):
            raise PlanStateError(
                "terminal task model steps must equal the last authoritative progress count"
            )
        if result.status == "succeeded" and set(self._task_model_steps) != expected_task_ids:
            raise PlanStateError(
                "succeeded terminal requires authoritative model-step progress for every task"
            )
        if result.status != "succeeded" and any(
            usage_by_task[task_id].model_steps != 0
            for task_id in expected_task_ids.difference(self._task_model_steps)
        ):
            raise PlanStateError(
                "a task without authoritative progress must report zero terminal model steps"
            )
        strategy = plan.execution_admission.execution_strategy
        worker_cap = min(strategy.max_parallel_workers, len(expected_task_ids))
        if result.workers.requested > worker_cap:
            raise PlanStateError("terminal worker allocation exceeds execution admission")
        if result.status == "succeeded" and result.workers.requested < 1:
            raise PlanStateError("succeeded run requires an admitted worker allocation")
        if (
            result.timing.elapsed_ms > plan.execution_admission.run_timeout_ms
            and not result.timing.timed_out
        ):
            raise PlanStateError("a run beyond its admitted timeout must be marked timedOut")

        if result.status == "succeeded":
            expected_acceptance = {
                check.check_id for check in contract.checks if check.domain == "acceptance"
            }
            expected_verification = {
                check.check_id for check in contract.checks if check.domain == "verification"
            }
            if {check.check_id for check in result.acceptance.checks} != expected_acceptance or {
                check.check_id for check in result.verification.checks
            } != expected_verification:
                raise ReferenceResolutionError(
                    "succeeded terminal must cover every approved acceptance and verification check"
                )

    def require_terminal(self) -> None:
        if not self._terminal_seen:
            raise TerminalEventError("the run ended without a terminal event")
