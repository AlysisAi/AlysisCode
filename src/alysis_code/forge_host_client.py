"""Presentation-neutral in-process client for the canonical local Forge Host.

CLI presentation uses this client directly. Electron and VS Code use the same
request models over :mod:`forge_host_stdio`; neither surface owns planner or
execution decisions.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from .forge_host_protocol.models import (
    ApprovalDecision,
    ApprovalDecisionValue,
    ApprovalRequired,
    EventEnvelope,
    Identity,
    PlannerAnswer,
    PlannerGoal,
    PlannerInput,
    PlannerQuestion,
    PlannerRevision,
    PlanReady,
    RequestEnvelope,
    RunStart,
)
from .forge_host_service import HostServiceOutcome


class LocalForgeHostService(Protocol):
    @property
    def identity(self) -> Identity: ...

    def handle_request(self, envelope: RequestEnvelope) -> HostServiceOutcome: ...


@dataclass(frozen=True, slots=True)
class SubmittedHostCommand:
    request: RequestEnvelope
    outcome: HostServiceOutcome


@dataclass(frozen=True, slots=True)
class PlannedHostGeneration:
    plan_ready: PlanReady
    approval_event: EventEnvelope
    commands: tuple[SubmittedHostCommand, ...]


def _opaque_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(16)}"


class ForgeHostInProcessClient:
    """Build and submit exact Host-v2 requests without presentation logic."""

    def __init__(
        self,
        service: LocalForgeHostService,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._service = service
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or _opaque_id

    @property
    def identity(self) -> Identity:
        return self._service.identity

    def _request(self, payload: object, *, operation_id: str | None = None) -> RequestEnvelope:
        operation = operation_id or self._id_factory("operation")
        return RequestEnvelope(
            protocol="alysis.forge-host",
            protocol_version="2.0",
            message_id=self._id_factory("msg"),
            operation_id=operation,
            sent_at=self._clock(),
            kind="request",
            request_id=self._id_factory("req"),
            idempotency_key=self._id_factory("idem"),
            payload=payload,
        )

    def submit(self, request: RequestEnvelope) -> SubmittedHostCommand:
        return SubmittedHostCommand(
            request=request,
            outcome=self._service.handle_request(request),
        )

    def plan_goal(self, text: str) -> SubmittedHostCommand:
        identity = self.identity
        return self.submit(
            self._request(
                PlannerInput(
                    type="planner.input",
                    identity=identity,
                    expected_revision=identity.revision,
                    input=PlannerGoal(kind="goal", text=text),
                )
            )
        )

    def plan_until_ready(
        self,
        text: str,
        *,
        answer_provider: Callable[[PlannerQuestion], str],
        max_questions: int = 16,
    ) -> PlannedHostGeneration:
        if max_questions < 0:
            raise ValueError("max_questions cannot be negative")
        commands = [self.plan_goal(text)]
        answered = 0
        while True:
            events = commands[-1].outcome.events
            plans = [event.payload for event in events if isinstance(event.payload, PlanReady)]
            approvals = [event for event in events if isinstance(event.payload, ApprovalRequired)]
            questions = [
                event.payload for event in events if isinstance(event.payload, PlannerQuestion)
            ]
            if len(plans) == 1 and len(approvals) == 1 and not questions:
                return PlannedHostGeneration(
                    plan_ready=plans[0],
                    approval_event=approvals[0],
                    commands=tuple(commands),
                )
            if plans or approvals or len(questions) != 1:
                raise RuntimeError("Host planner returned an invalid presentation batch")
            if answered >= max_questions:
                raise RuntimeError("Host planner exceeded the clarification limit")
            question = questions[0]
            answer = answer_provider(question).strip()
            if not answer:
                raise ValueError("Host planner question requires an answer")
            commands.append(self.answer(question_id=question.question_id, text=answer))
            answered += 1

    def answer(self, *, question_id: str, text: str) -> SubmittedHostCommand:
        identity = self.identity
        return self.submit(
            self._request(
                PlannerInput(
                    type="planner.input",
                    identity=identity,
                    expected_revision=identity.revision,
                    input=PlannerAnswer(kind="answer", question_id=question_id, text=text),
                )
            )
        )

    def revise(self, text: str) -> SubmittedHostCommand:
        identity = self.identity
        return self.submit(
            self._request(
                PlannerInput(
                    type="planner.input",
                    identity=identity,
                    expected_revision=identity.revision,
                    input=PlannerRevision(kind="revision", text=text),
                )
            )
        )

    def decide(
        self,
        approval_event: EventEnvelope,
        *,
        decision: ApprovalDecisionValue,
    ) -> SubmittedHostCommand:
        approval = approval_event.payload
        if not isinstance(approval, ApprovalRequired):
            raise TypeError("approval decision requires an approval.required event")
        if approval_event.operation_id is None:
            raise ValueError("approval.required event has no operation authority")
        identity = self.identity
        return self.submit(
            self._request(
                ApprovalDecision(
                    type="approval.decision",
                    identity=identity,
                    expected_revision=identity.revision,
                    approval_operation_id=approval_event.operation_id,
                    approval_id=approval.approval_id,
                    action_fingerprint=approval.action_fingerprint,
                    scope_fingerprint=approval.scope.fingerprint,
                    expires_at=approval.expires_at,
                    decision=decision,
                )
            )
        )

    def start(self, plan_ready: PlanReady) -> SubmittedHostCommand:
        identity = self.identity
        return self.submit(
            self._request(
                RunStart(
                    type="run.start",
                    identity=identity,
                    expected_revision=identity.revision,
                    host_plan_fingerprint=plan_ready.host_plan_fingerprint,
                    native_approval_fingerprint=plan_ready.native_approval_fingerprint,
                    verification_contract_fingerprint=(
                        plan_ready.verification_contract.contract_fingerprint
                    ),
                    execution_admission=plan_ready.execution_admission,
                )
            )
        )


__all__ = [
    "ForgeHostInProcessClient",
    "LocalForgeHostService",
    "PlannedHostGeneration",
    "SubmittedHostCommand",
]
