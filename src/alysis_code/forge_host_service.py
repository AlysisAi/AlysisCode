"""In-memory single-writer service for the first Forge Host-v2 slice.

The service owns protocol sequencing, idempotency, approval authority, Host run
identity allocation, and replay bytes.  Native planning and execution remain
injected engine seams; returned execution facts are revalidated before they can
become protocol evidence or terminal truth.
"""

from __future__ import annotations

import copy
import hashlib
import os
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol

from .command_text import command_text
from .config import AppConfig
from .execution_deadline import ExecutionDeadline
from .forge import RunPaths, load_plan
from .forge_host_native_adapter import (
    ForgeHostPreflightError,
    IntentKind,
    NativeAdapterDependencies,
    NativeAdapterRequest,
    NativePlanAdmission,
    ProviderBinding,
    PytestExecutableBinding,
    ScopeKind,
    build_native_plan_admission,
    private_native_plan_admission,
    revalidate_native_plan_admission,
    revalidate_pytest_executable_binding,
)
from .forge_host_protocol.canonical import approval_action_fingerprint
from .forge_host_protocol.codec import encode_envelope, semantic_payload_digest, validate_envelope
from .forge_host_protocol.errors import (
    ApprovalStateError,
    IdempotencyConflictError,
    ReplayUnavailableError,
)
from .forge_host_protocol.ledger import ProtocolLedger, RequestDisposition
from .forge_host_protocol.models import (
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    ApprovalDecision,
    ApprovalRequired,
    ArtifactRecord,
    CheckResult,
    CommandAccepted,
    EventEnvelope,
    EvidenceReady,
    EvidenceRecord,
    Identity,
    PlanExecuteAction,
    PlannerGoal,
    PlannerInput,
    PlannerQuestion,
    PlannerRevision,
    PlanReady,
    RequestEnvelope,
    ResponseEnvelope,
    RunProgress,
    RunStart,
    RunTerminal,
    TaskExecutionUsage,
    TerminalReason,
    TerminalResult,
    Timing,
    Usage,
    VerificationResult,
    Workers,
)
from .forge_planner_engine import (
    ForgePlannerDependencies,
    ForgePlannerFinalizeRequest,
    ForgePlannerState,
    ForgePlannerTurnRequest,
    finalize_forge_planner_plan,
    run_forge_planner_turn,
)
from .forge_planning_profile import PlanningProfile, normalize_planning_profile
from .git_safe import build_git_cmd, build_git_process_env


@dataclass(frozen=True)
class HostPlannerRequest:
    planner_input: PlannerInput
    operation_id: str
    identity: Identity


@dataclass(frozen=True)
class HostPlannerOutcome:
    question: str | None = None
    admission: NativePlanAdmission | None = None

    def __post_init__(self) -> None:
        if (self.question is None) == (self.admission is None):
            raise ValueError("planner outcome must contain exactly one question or admission")


class HostPlanner(Protocol):
    def __call__(self, request: HostPlannerRequest) -> HostPlannerOutcome: ...


@dataclass(frozen=True)
class ForgeHostPlannerAdapterSettings:
    workspace_ref: str
    provider_binding: ProviderBinding
    per_managed_task_max_steps: int
    run_timeout_ms: int
    trusted_pytest: Path
    intent_kind: IntentKind | None = None
    verification_commands: tuple[str, ...] | None = None
    declared_scope_kinds: Mapping[str, ScopeKind] = field(default_factory=dict)
    api_key_override: str | None = field(default=None, repr=False)
    selection_label: str = "host-v2-planner"
    planning_profile: PlanningProfile = "quick"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "verification_commands",
            (None if self.verification_commands is None else tuple(self.verification_commands)),
        )
        object.__setattr__(
            self,
            "declared_scope_kinds",
            MappingProxyType(dict(self.declared_scope_kinds)),
        )
        object.__setattr__(
            self,
            "planning_profile",
            normalize_planning_profile(self.planning_profile),
        )


class ForgeHostPlannerAdapter:
    """Concrete Host planner backed by the native UI-neutral planner engine."""

    def __init__(
        self,
        *,
        paths: RunPaths,
        plan: dict[str, object],
        state: ForgePlannerState,
        settings: ForgeHostPlannerAdapterSettings,
        cfg_loader: Callable[[], AppConfig],
        planner_dependencies: ForgePlannerDependencies | None = None,
        native_dependencies: NativeAdapterDependencies | None = None,
        path_validator: Callable[[RunPaths, dict[str, object]], list[str]] | None = None,
        unavailable_message_builder: Callable[[Exception], str] | None = None,
    ) -> None:
        self._paths = paths
        self._plan = copy.deepcopy(plan)
        self._state = copy.deepcopy(state)
        self._settings = ForgeHostPlannerAdapterSettings(
            workspace_ref=settings.workspace_ref,
            provider_binding=copy.deepcopy(settings.provider_binding),
            per_managed_task_max_steps=settings.per_managed_task_max_steps,
            run_timeout_ms=settings.run_timeout_ms,
            trusted_pytest=Path(settings.trusted_pytest),
            intent_kind=settings.intent_kind,
            verification_commands=settings.verification_commands,
            declared_scope_kinds=settings.declared_scope_kinds,
            api_key_override=settings.api_key_override,
            selection_label=settings.selection_label,
            planning_profile=settings.planning_profile,
        )
        self._cfg_loader = cfg_loader
        self._planner_dependencies = planner_dependencies
        self._native_dependencies = native_dependencies
        self._path_validator = path_validator
        self._unavailable_message_builder = unavailable_message_builder or (
            lambda _error: "The planner is unavailable for this Forge generation."
        )
        self._explicit_intent_input: str | None = None

    @property
    def state(self) -> ForgePlannerState:
        return copy.deepcopy(self._state)

    @property
    def native_plan(self) -> dict[str, object]:
        return copy.deepcopy(self._plan)

    def __call__(self, request: HostPlannerRequest) -> HostPlannerOutcome:
        planner_value = request.planner_input.input
        user_text = planner_value.text
        if isinstance(planner_value, PlannerGoal | PlannerRevision):
            self._explicit_intent_input = user_text.strip()
        if not self._explicit_intent_input:
            raise ValueError("Host planning must begin with an explicit goal or revision")

        turn_plan = copy.deepcopy(self._plan)
        turn_state = copy.deepcopy(self._state)
        outcome = run_forge_planner_turn(
            ForgePlannerTurnRequest(
                paths=self._paths,
                plan=turn_plan,
                state=turn_state,
                user_text=user_text,
                cfg_loader=self._cfg_loader,
                unavailable_message_builder=self._unavailable_message_builder,
                api_key_override=self._settings.api_key_override,
                selection_label=self._settings.selection_label,
                planning_relevant=True,
                refresh_workspace_context=False,
                planning_profile=self._settings.planning_profile,
            ),
            dependencies=self._planner_dependencies,
        )
        # Detach immediately from objects handed across the provider seam.  A
        # provider retaining those references cannot mutate later authority.
        self._plan = copy.deepcopy(turn_plan)
        self._state = copy.deepcopy(turn_state)
        if outcome.status in {"error", "unavailable"}:
            raise RuntimeError("the native planner did not produce an admissible result")
        if outcome.questions and outcome.apply_result is None:
            return HostPlannerOutcome(question=outcome.questions[0])
        tasks = self._plan.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise RuntimeError("the native planner did not produce an executable task")

        # The planner engine persists guarded application.  Reload that
        # canonical native authority before finalization and Host projection.
        self._plan = copy.deepcopy(load_plan(self._paths))
        finalize_forge_planner_plan(
            ForgePlannerFinalizeRequest(
                paths=self._paths,
                plan=self._plan,
                transcript_tail=list(self._state.transcript),
            ),
            dependencies=self._planner_dependencies,
            path_validator=self._path_validator,
        )
        admission = build_native_plan_admission(
            NativeAdapterRequest(
                paths=self._paths,
                plan=self._plan,
                planner_input=self._explicit_intent_input,
                workspace_ref=self._settings.workspace_ref,
                provider_binding=self._settings.provider_binding,
                per_managed_task_max_steps=self._settings.per_managed_task_max_steps,
                run_timeout_ms=self._settings.run_timeout_ms,
                intent_kind=self._settings.intent_kind,
                verification_commands=self._settings.verification_commands,
                trusted_pytest=self._settings.trusted_pytest,
                declared_scope_kinds=self._settings.declared_scope_kinds,
            ),
            dependencies=self._native_dependencies,
        )
        return HostPlannerOutcome(admission=admission)


@dataclass(frozen=True)
class HostSequentialProgress:
    phase: Literal["preflight", "executing", "verifying", "reviewing", "finalizing"]
    summary: str
    task_id: str | None = None
    step: int = 0
    total_steps: int = 0
    task_model_steps: int | None = None
    worker_id: str | None = None

    def to_payload(self) -> RunProgress:
        return RunProgress(
            type="run.progress",
            phase=self.phase,
            task_id=self.task_id,
            step=self.step,
            total_steps=self.total_steps,
            task_model_steps=self.task_model_steps,
            summary=self.summary,
            worker_id=self.worker_id,
        )


@dataclass(frozen=True)
class HostSequentialRunRequest:
    native_paths: RunPaths
    native_plan: Mapping[str, object]
    native_admission: NativePlanAdmission
    plan_ready: PlanReady
    host_run_id: str
    native_run_id: str
    operation_id: str
    base_branch: str
    base_head: str
    pytest_executable: PytestExecutableBinding
    started_at: datetime
    execution_deadline: ExecutionDeadline
    reload_native_plan_under_guard: Callable[[], dict[str, object]]
    register_prepared_integration: Callable[[HostPreparedIntegration], None] | None = None


@dataclass(frozen=True)
class HostIntegrationCommitProof:
    """Private proof for the irreversible Phase-0 Git integration boundary."""

    base_branch: str
    approved_base_head: str
    task_commit: str
    merge_commit: str
    integration_committed_elapsed_ms: int


@dataclass(frozen=True)
class HostIntegrationFinalizeProof:
    """Private whole-run timing authority retained through Host publication."""

    commit_proof: HostIntegrationCommitProof
    final_elapsed_ms: int


class HostPreparedIntegrationFacts(Protocol):
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


class HostPreparedIntegration(Protocol):
    """Opaque reversible integration authority registered before translation."""

    @property
    def facts(self) -> HostPreparedIntegrationFacts: ...

    @property
    def state(self) -> str: ...

    @property
    def guard_release_pending(self) -> bool: ...

    def commit_reversible(self) -> HostIntegrationCommitProof: ...

    def rollback(self, reason: str) -> None: ...

    def abort(self, reason: str) -> None: ...

    def finalize_reversible(self) -> HostIntegrationFinalizeProof: ...

    def complete_publish(
        self,
        *,
        event_bytes_digest: str,
        event_bytes: int,
    ) -> HostIntegrationFinalizeProof: ...

    def retry_guard_release(self) -> bool: ...


@dataclass(frozen=True)
class HostSequentialRunResult:
    progress: tuple[HostSequentialProgress, ...]
    evidence: tuple[EvidenceReady, ...]
    terminal: TerminalResult
    prepared_integration: HostPreparedIntegration | None = None
    # Temporary compatibility during the local transaction migration.  The
    # final vertical slice removes already-committed runner results entirely.
    integration_commit_proof: HostIntegrationCommitProof | None = None


class HostSequentialRunner(Protocol):
    def __call__(
        self,
        request: HostSequentialRunRequest,
        *,
        progress_sink: Callable[[HostSequentialProgress], None] | None = None,
    ) -> HostSequentialRunResult: ...


@dataclass(frozen=True)
class HostServiceOutcome:
    response: ResponseEnvelope
    events: tuple[EventEnvelope, ...]
    response_bytes: bytes
    event_bytes: tuple[bytes, ...]
    replayed: bool


@dataclass(frozen=True)
class _OperationReplay:
    semantic_digest: str
    event_bytes: tuple[bytes, ...]


@dataclass(frozen=True)
class _PreparedEventAppend:
    """Fully validated replacement state for one atomic in-memory publish.

    Event codec and ledger validation are deliberately completed before any
    authoritative service collection is mutated.  Publishing this object is
    therefore limited to reference assignments, which is the primitive used
    by the reversible integration transaction at its final commit boundary.
    """

    ledger: ProtocolLedger
    events: tuple[EventEnvelope, ...]
    event_bytes: tuple[bytes, ...]
    all_events: list[EventEnvelope]
    all_event_bytes: list[bytes]


@dataclass(frozen=True)
class _PreparedRunPublication:
    """Complete no-side-effect publication image for a terminal run batch."""

    event_append: _PreparedEventAppend
    operation_replays: dict[str, _OperationReplay]
    outcome: HostServiceOutcome


@dataclass(frozen=True)
class _ServicePublicationSnapshot:
    ledger: ProtocolLedger
    events: list[EventEnvelope]
    event_bytes: list[bytes]
    operation_replays: dict[str, _OperationReplay]
    phase: Literal["planning", "awaiting_approval", "ready", "running", "terminal"]


AdmissionRevalidator = Callable[[NativePlanAdmission], dict[str, object]]


class ForgeHostService:
    """One authoritative in-memory generation, serialized by a re-entrant lock."""

    def __init__(
        self,
        *,
        identity: Identity,
        planner: HostPlanner,
        sequential_runner: HostSequentialRunner,
        current_provider_binding: Callable[[], ProviderBinding],
        admission_revalidator: AdmissionRevalidator = revalidate_native_plan_admission,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        approval_ttl: timedelta = timedelta(minutes=5),
        host_run_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if identity.run_id is not None:
            raise ValueError("ForgeHostService must start with a pre-run identity")
        self._lock = threading.RLock()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self._approval_ttl = approval_ttl
        self._planner = planner
        self._sequential_runner = sequential_runner
        self._current_provider_binding = current_provider_binding
        self._admission_revalidator = admission_revalidator
        self._host_run_id_factory = host_run_id_factory
        self._ledger = ProtocolLedger(identity, clock=self._clock)
        self._events: list[EventEnvelope] = []
        self._event_bytes: list[bytes] = []
        self._operation_replays: dict[str, _OperationReplay] = {}
        self._admission: NativePlanAdmission | None = None
        self._phase: Literal["planning", "awaiting_approval", "ready", "running", "terminal"] = (
            "planning"
        )
        self._id_counter = 0
        self._allocated_host_run_id: str | None = None
        self._pending_guard_releases: list[HostPreparedIntegration] = []

    @property
    def identity(self) -> Identity:
        with self._lock:
            return self._private_identity(self._ledger.identity)

    @property
    def phase(self) -> str:
        with self._lock:
            return self._phase

    @property
    def admission(self) -> NativePlanAdmission | None:
        with self._lock:
            return (
                None if self._admission is None else private_native_plan_admission(self._admission)
            )

    @property
    def pending_guard_release_count(self) -> int:
        with self._lock:
            return len(self._pending_guard_releases)

    def _retry_pending_guard_releases(self) -> None:
        retained: list[HostPreparedIntegration] = []
        for transaction in self._pending_guard_releases:
            try:
                released = transaction.retry_guard_release()
            except Exception:  # noqa: BLE001 - durable success remains authoritative
                released = False
            if not released or transaction.guard_release_pending:
                retained.append(transaction)
        self._pending_guard_releases = retained

    @staticmethod
    def _private_identity(identity: Identity) -> Identity:
        return Identity.model_validate_json(identity.model_dump_json(by_alias=True), strict=True)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("service clock must return an aware datetime")
        return value

    def _safe_now(self, fallback: datetime) -> datetime:
        """Return audit wall time without endangering an accepted operation.

        Run deadlines use the injected monotonic clock.  Once ``run.start`` is
        committed, a wall-clock outage may degrade event timestamps to the
        already validated start time, but it must never strand authority or
        suppress the one terminal/replay record.
        """

        try:
            return max(self._now(), fallback)
        except Exception:  # noqa: BLE001 - post-commit audit metadata only
            return fallback

    def _new_id(self, prefix: str) -> str:
        self._id_counter += 1
        authority = (
            f"{self._ledger.identity.session_id}:{self._ledger.identity.generation_id}:"
            f"{prefix}:{self._id_counter}"
        ).encode()
        suffix = hashlib.sha256(authority).hexdigest()[:24]
        return f"{prefix}_{suffix}"

    def _candidate_host_run_id(self) -> str:
        if self._allocated_host_run_id is not None:
            raise RuntimeError("Host run ID allocation is one-shot")
        candidate = (
            self._host_run_id_factory()
            if self._host_run_id_factory is not None
            else self._new_id("run")
        )
        if not isinstance(candidate, str) or not candidate.startswith("run_"):
            raise ValueError("host_run_id_factory returned an invalid Host run ID")
        return candidate

    def _assert_current_provider(self, admission: NativePlanAdmission) -> None:
        current = self._current_provider_binding()
        if not isinstance(current, ProviderBinding) or current != admission.provider_binding:
            raise ForgeHostPreflightError(
                "provider_binding_changed",
                "The provider binding changed after approval.",
            )

    @staticmethod
    def _private_request(envelope: RequestEnvelope) -> RequestEnvelope:
        validated = validate_envelope(encode_envelope(envelope))
        if not isinstance(validated, RequestEnvelope):
            raise TypeError("request codec isolation returned the wrong envelope type")
        return validated

    @staticmethod
    def _private_event(envelope: EventEnvelope) -> EventEnvelope:
        validated = validate_envelope(encode_envelope(envelope))
        if not isinstance(validated, EventEnvelope):
            raise TypeError("event codec isolation returned the wrong envelope type")
        return validated

    def _event(
        self,
        *,
        payload: object,
        operation_id: str,
        identity: Identity,
        sequence: int,
        sent_at: datetime | None = None,
    ) -> EventEnvelope:
        event = EventEnvelope(
            protocol=PROTOCOL_NAME,
            protocol_version=PROTOCOL_VERSION,
            message_id=self._new_id("msg"),
            operation_id=operation_id,
            sent_at=sent_at if sent_at is not None else self._now(),
            kind="event",
            sequence=sequence,
            identity=identity,
            payload=payload,
        )
        return self._private_event(event)

    def _prepare_event_append(
        self,
        events: tuple[EventEnvelope, ...],
    ) -> _PreparedEventAppend:
        shadow = copy.deepcopy(self._ledger)
        private_events: list[EventEnvelope] = []
        wire_events: list[bytes] = []
        for event in events:
            private = self._private_event(event)
            shadow.accept_event(private)
            private_events.append(private)
            wire_events.append(encode_envelope(private))
        private_tuple = tuple(private_events)
        wire_tuple = tuple(wire_events)
        return _PreparedEventAppend(
            ledger=shadow,
            events=private_tuple,
            event_bytes=wire_tuple,
            all_events=[*self._events, *private_tuple],
            all_event_bytes=[*self._event_bytes, *wire_tuple],
        )

    def _publish_event_append(self, prepared: _PreparedEventAppend) -> None:
        # Every fallible operation (codec validation, ledger transitions and
        # list allocation) happened in ``_prepare_event_append``.  Keep this
        # boundary to plain reference assignments so a reversible Git
        # integration can be sealed immediately before it.
        self._ledger = prepared.ledger
        self._events = prepared.all_events
        self._event_bytes = prepared.all_event_bytes

    def _append_events(self, events: tuple[EventEnvelope, ...]) -> tuple[bytes, ...]:
        prepared = self._prepare_event_append(events)
        self._publish_event_append(prepared)
        return prepared.event_bytes

    def _prepare_run_publication(
        self,
        *,
        request: RequestEnvelope,
        events: tuple[EventEnvelope, ...],
    ) -> _PreparedRunPublication:
        """Build terminal ledger/replay/response state without mutating service truth."""

        if request.operation_id is None:
            raise ValueError("run publication requires an operation ID")
        prepared_events = self._prepare_event_append(events)
        replay = _OperationReplay(
            semantic_digest=semantic_payload_digest(request),
            event_bytes=prepared_events.event_bytes,
        )
        next_replays = dict(self._operation_replays)
        next_replays[request.operation_id] = replay
        response, response_bytes = self._accepted_response(
            request,
            duplicate=False,
            ledger=prepared_events.ledger,
        )
        outcome = HostServiceOutcome(
            response=response,
            events=prepared_events.events,
            response_bytes=response_bytes,
            event_bytes=prepared_events.event_bytes,
            replayed=False,
        )
        return _PreparedRunPublication(
            event_append=prepared_events,
            operation_replays=next_replays,
            outcome=outcome,
        )

    @staticmethod
    def _publication_digest(event_bytes: tuple[bytes, ...]) -> str:
        digest = hashlib.sha256()
        for value in event_bytes:
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
        return digest.hexdigest()

    def _publish_run_publication(self, prepared: _PreparedRunPublication) -> None:
        """Publish a preallocated run image using only reference assignments."""

        self._publish_event_append(prepared.event_append)
        self._operation_replays = prepared.operation_replays
        self._phase = "terminal"

    def _snapshot_publication_state(self) -> _ServicePublicationSnapshot:
        return _ServicePublicationSnapshot(
            ledger=self._ledger,
            events=self._events,
            event_bytes=self._event_bytes,
            operation_replays=self._operation_replays,
            phase=self._phase,
        )

    def _restore_publication_state(self, snapshot: _ServicePublicationSnapshot) -> None:
        # Like publication, restoration is a lock-held series of plain
        # reference assignments.  No partially staged run is observable to a
        # caller because ``handle_request`` retains the service RLock.
        self._ledger = snapshot.ledger
        self._events = snapshot.events
        self._event_bytes = snapshot.event_bytes
        self._operation_replays = snapshot.operation_replays
        self._phase = snapshot.phase

    def _accepted_response(
        self,
        request: RequestEnvelope,
        *,
        duplicate: bool,
        ledger: ProtocolLedger | None = None,
    ) -> tuple[ResponseEnvelope, bytes]:
        response_ledger = ledger or self._ledger
        response = ResponseEnvelope(
            protocol=PROTOCOL_NAME,
            protocol_version=PROTOCOL_VERSION,
            message_id=self._new_id("msg"),
            operation_id=request.operation_id,
            sent_at=self._safe_now(request.sent_at),
            kind="response",
            reply_to=request.request_id,
            payload=CommandAccepted(
                type="command.accepted",
                identity=response_ledger.identity,
                duplicate=duplicate,
            ),
        )
        validated = validate_envelope(encode_envelope(response))
        if not isinstance(validated, ResponseEnvelope):
            raise TypeError("response codec isolation returned the wrong envelope type")
        return validated, encode_envelope(validated)

    def _outcome(
        self,
        request: RequestEnvelope,
        *,
        events: tuple[EventEnvelope, ...],
        event_bytes: tuple[bytes, ...],
        replayed: bool,
    ) -> HostServiceOutcome:
        response, response_bytes = self._accepted_response(request, duplicate=replayed)
        return HostServiceOutcome(
            response=response,
            events=events,
            response_bytes=response_bytes,
            event_bytes=event_bytes,
            replayed=replayed,
        )

    @staticmethod
    def _decode_events(values: tuple[bytes, ...]) -> tuple[EventEnvelope, ...]:
        events: list[EventEnvelope] = []
        for value in values:
            envelope = validate_envelope(value)
            if not isinstance(envelope, EventEnvelope):
                raise TypeError("stored replay bytes are not an event")
            events.append(envelope)
        return tuple(events)

    def handle_request(self, envelope: RequestEnvelope) -> HostServiceOutcome:
        """Accept one request and return only newly emitted or replayed messages."""

        request = self._private_request(envelope)
        if request.operation_id is None:
            raise ValueError("handshake is negotiated outside this generation service")
        with self._lock:
            self._retry_pending_guard_releases()
            cached = self._operation_replays.get(request.operation_id)
            if cached is not None:
                if semantic_payload_digest(request) != cached.semantic_digest:
                    raise IdempotencyConflictError(
                        "operationId was already used for a different command"
                    )
                events = self._decode_events(cached.event_bytes)
                return self._outcome(
                    request,
                    events=events,
                    event_bytes=cached.event_bytes,
                    replayed=True,
                )
            shadow = copy.deepcopy(self._ledger)
            disposition = shadow.accept_request(request)
            if disposition == RequestDisposition.REPLAY:
                # Commit any new idempotency-key alias to the authoritative ledger.
                self._ledger.accept_request(request)
                replay = self._operation_replays.get(request.operation_id)
                if replay is None:
                    raise RuntimeError("accepted operation replay has no stored result")
                events = self._decode_events(replay.event_bytes)
                return self._outcome(
                    request,
                    events=events,
                    event_bytes=replay.event_bytes,
                    replayed=True,
                )

            payload = request.payload
            if isinstance(payload, PlannerInput):
                return self._handle_planner(request, shadow)
            if isinstance(payload, ApprovalDecision):
                return self._handle_approval(request, shadow)
            if isinstance(payload, RunStart):
                return self._handle_run_start(request, shadow)
            raise ValueError("request type is unavailable in the first Host vertical slice")

    def _handle_planner(
        self,
        request: RequestEnvelope,
        shadow: ProtocolLedger,
    ) -> HostServiceOutcome:
        assert isinstance(request.payload, PlannerInput)
        assert request.operation_id is not None
        planner_outcome = self._planner(
            HostPlannerRequest(
                planner_input=request.payload,
                operation_id=request.operation_id,
                identity=self._private_identity(shadow.identity),
            )
        )
        event_identity = Identity(
            session_id=shadow.identity.session_id,
            generation_id=shadow.identity.generation_id,
            run_id=None,
            revision=shadow.identity.revision + 1,
        )
        sequence = shadow.last_sequence + 1
        events: list[EventEnvelope] = []
        next_admission: NativePlanAdmission | None = None
        next_phase: Literal["planning", "awaiting_approval"]
        if planner_outcome.question is not None:
            question = planner_outcome.question.strip()
            if not question:
                raise ValueError("planner question cannot be blank")
            events.append(
                self._event(
                    payload=PlannerQuestion(
                        type="planner.question",
                        question_id=self._new_id("question"),
                        prompt=question,
                        required=True,
                    ),
                    operation_id=request.operation_id,
                    identity=event_identity,
                    sequence=sequence,
                )
            )
            next_phase = "planning"
        else:
            untrusted_admission = planner_outcome.admission
            assert untrusted_admission is not None
            admission = private_native_plan_admission(untrusted_admission)
            self._assert_current_provider(admission)
            if self._admission is not None:
                raise RuntimeError("a generation cannot replace its immutable plan")
            plan_event = self._event(
                payload=admission.plan_ready,
                operation_id=request.operation_id,
                identity=event_identity,
                sequence=sequence,
            )
            shadow.accept_event(plan_event)
            events.append(plan_event)
            approval_event = self._approval_event(
                ledger=shadow,
                identity=event_identity,
                sequence=sequence + 1,
            )
            events.append(approval_event)
            next_admission = admission
            next_phase = "awaiting_approval"

        # Validate the complete batch on the request-bearing shadow before the
        # authoritative request/event stream advances.
        validation_shadow = copy.deepcopy(self._ledger)
        validation_shadow.accept_request(request)
        for event in events:
            validation_shadow.accept_event(event)
        self._ledger.accept_request(request)
        event_bytes = self._append_events(tuple(events))
        self._admission = next_admission or self._admission
        self._phase = next_phase
        self._operation_replays[request.operation_id] = _OperationReplay(
            semantic_digest=semantic_payload_digest(request),
            event_bytes=event_bytes,
        )
        return self._outcome(
            request,
            events=tuple(events),
            event_bytes=event_bytes,
            replayed=False,
        )

    def _approval_event(
        self,
        *,
        ledger: ProtocolLedger,
        identity: Identity,
        sequence: int,
    ) -> EventEnvelope:
        if self._admission is not None:
            ready = self._admission.plan_ready
        else:
            # During the atomic planner batch, authority lives on the shadow.
            plan = getattr(ledger, "_plan", None)
            if plan is None:
                raise RuntimeError("approval issuance requires plan.ready authority")
            ready = plan
        policy = ready.approval_policy
        if policy.scope is None or policy.risk is None or policy.summary is None:
            raise RuntimeError("Phase-0 plan approval policy is incomplete")
        operation_id = self._new_id("operation")
        approval_id = self._new_id("approval")
        expires_at = (
            (self._now() + self._approval_ttl).astimezone(timezone.utc).replace(microsecond=0)
        )
        action = PlanExecuteAction(
            kind="plan_execute",
            host_plan_fingerprint=ready.host_plan_fingerprint,
            native_approval_fingerprint=ready.native_approval_fingerprint,
            verification_contract_fingerprint=ready.verification_contract.contract_fingerprint,
            execution_admission_fingerprint=ready.execution_admission.fingerprint,
            approval_policy_fingerprint=policy.fingerprint,
        )
        expires_wire = expires_at.isoformat().replace("+00:00", "Z")
        action_fingerprint = approval_action_fingerprint(
            operation_id=operation_id,
            session_id=identity.session_id,
            generation_id=identity.generation_id,
            run_id=identity.run_id,
            revision=identity.revision,
            approval_id=approval_id,
            action=action.model_dump(mode="json", by_alias=True, exclude_none=False),
            scope_fingerprint=policy.scope.fingerprint,
            expires_at=expires_wire,
            risk=policy.risk,
            summary=policy.summary,
        )
        return self._event(
            payload=ApprovalRequired(
                type="approval.required",
                approval_id=approval_id,
                action=action,
                action_fingerprint=action_fingerprint,
                summary=policy.summary,
                risk=policy.risk,
                scope=policy.scope,
                expires_at=expires_at,
            ),
            operation_id=operation_id,
            identity=identity,
            sequence=sequence,
        )

    def _handle_approval(
        self,
        request: RequestEnvelope,
        shadow: ProtocolLedger,
    ) -> HostServiceOutcome:
        assert isinstance(request.payload, ApprovalDecision)
        assert request.operation_id is not None
        self._ledger.accept_request(request)
        self._phase = "ready"
        event_bytes: tuple[bytes, ...] = ()
        self._operation_replays[request.operation_id] = _OperationReplay(
            semantic_digest=semantic_payload_digest(request),
            event_bytes=event_bytes,
        )
        return self._outcome(
            request,
            events=(),
            event_bytes=event_bytes,
            replayed=False,
        )

    def reissue_plan_approval(self) -> tuple[EventEnvelope, ...]:
        """Issue fresh authority after denial or expiry without redefining the plan."""

        with self._lock:
            if self._admission is None or self._phase not in {"ready", "awaiting_approval"}:
                raise ApprovalStateError("plan approval cannot be issued in the current phase")
            identity = self._private_identity(self._ledger.identity)
            event = self._approval_event(
                ledger=self._ledger,
                identity=identity,
                sequence=self._ledger.last_sequence + 1,
            )
            self._append_events((event,))
            self._phase = "awaiting_approval"
            return (event,)

    def _handle_run_start(
        self,
        request: RequestEnvelope,
        accepted_shadow: ProtocolLedger,
    ) -> HostServiceOutcome:
        assert isinstance(request.payload, RunStart)
        assert request.operation_id is not None
        admission = self._admission
        if admission is None:
            raise RuntimeError("run.start has no native admission")

        # The request has been accepted only on the transaction shadow at this
        # point.  Validate the complete Host identity and bind it there before
        # committing either the one-shot authority or the allocated run ID.
        # A bad factory result is therefore safely retryable with the identical
        # run.start request.
        host_run_id = self._candidate_host_run_id()
        run_identity = Identity(
            session_id=accepted_shadow.identity.session_id,
            generation_id=accepted_shadow.identity.generation_id,
            run_id=host_run_id,
            revision=accepted_shadow.identity.revision + 1,
        )
        accepted_shadow.bind_run(run_identity)
        started_at = self._now()
        execution_deadline = ExecutionDeadline.from_duration(
            admission.plan_ready.execution_admission.run_timeout_ms / 1000.0,
            clock=self._monotonic,
            source="forge_host_v2",
        )
        # Commit only after every fallible run-boundary value is constructed.
        # A clock/deadline failure leaves the allow-once request unconsumed and
        # permits an identical retry without a stranded Host run identity.
        self._allocated_host_run_id = host_run_id
        self._ledger = accepted_shadow
        self._phase = "running"
        streamed_progress: list[HostSequentialProgress] = []
        private_progress: tuple[RunProgress, ...] = ()
        observed_terminal: TerminalResult | None = None
        registered_integration: HostPreparedIntegration | None = None
        rollback_failed = False

        def _register_prepared_integration(value: HostPreparedIntegration) -> None:
            nonlocal registered_integration
            if registered_integration is not None:
                raise RuntimeError("prepared integration registration is one-shot")
            facts = value.facts
            if value.state != "PREPARED":
                raise ValueError("prepared integration registered in the wrong state")
            if (
                facts.native_run_id != admission.native_run_id
                or facts.base_branch != admission.workspace_git.base_branch
                or facts.approved_base_head != admission.workspace_git.base_head
            ):
                raise ValueError("prepared integration authority does not match admission")
            approved_tasks = admission.plan_ready.plan_definition.tasks
            native_tasks = admission.native_plan_snapshot.get("tasks")
            if (
                len(approved_tasks) != 1
                or not isinstance(native_tasks, Sequence)
                or len(native_tasks) != 1
                or not isinstance(native_tasks[0], Mapping)
            ):
                raise ValueError("prepared integration requires one exact admitted task")
            approved_task = approved_tasks[0]
            native_branch = str(native_tasks[0].get("branch") or "").strip()
            transaction_suffix = facts.transaction_id.removeprefix("integration_")
            if (
                facts.task_id != approved_task.task_id
                or facts.task_branch != approved_task.branch
                or facts.task_branch != native_branch
                or len(transaction_suffix) != 24
                or any(character not in "0123456789abcdef" for character in transaction_suffix)
                or facts.prepared_ref != f"refs/alysis/forge-host-v2/{facts.transaction_id}"
            ):
                raise ValueError("prepared integration task authority does not match admission")
            commit_ids = (facts.task_commit, facts.prepared_merge_commit)
            if any(
                len(commit_id) not in {40, 64}
                or commit_id != commit_id.lower()
                or any(character not in "0123456789abcdef" for character in commit_id)
                for commit_id in commit_ids
            ):
                raise ValueError("prepared integration contains an invalid commit identity")
            timeout_ms = admission.plan_ready.execution_admission.run_timeout_ms
            if (
                isinstance(facts.prepared_elapsed_ms, bool)
                or not isinstance(facts.prepared_elapsed_ms, int)
                or facts.prepared_elapsed_ms < 0
                or facts.prepared_elapsed_ms >= timeout_ms
            ):
                raise ValueError("prepared integration exceeded the admitted deadline")
            expected_journal_dir = admission.paths.execution_integration_dir / "transactions"
            try:
                workspace_root = admission.workspace_git.workspace_root.resolve(strict=True)
                journal_path = facts.journal_path
                journal_path.relative_to(workspace_root)
                relative = journal_path.relative_to(workspace_root)
                cursor = workspace_root
                for component in relative.parts:
                    cursor = cursor / component
                    metadata = os.lstat(cursor)
                    if stat.S_ISLNK(metadata.st_mode):
                        raise ValueError("prepared integration journal has a symlink ancestor")
                resolved_journal = journal_path.resolve(strict=True)
                resolved_directory = expected_journal_dir.resolve(strict=True)
            except (OSError, ValueError) as exc:
                raise ValueError("prepared integration journal is not safely retained") from exc
            if (
                resolved_journal.parent != resolved_directory
                or journal_path.name != f"{facts.transaction_id}.json"
                or not stat.S_ISREG(os.lstat(resolved_journal).st_mode)
            ):
                raise ValueError("prepared integration journal authority is invalid")
            root = admission.workspace_git.git_root
            if (
                self._git_output(
                    root,
                    ["rev-parse", "--verify", f"refs/heads/{facts.base_branch}^{{commit}}"],
                )
                != facts.approved_base_head
                or self._git_output(
                    root,
                    ["rev-parse", "--verify", f"refs/heads/{facts.task_branch}^{{commit}}"],
                )
                != facts.task_commit
                or self._git_output(
                    root,
                    ["rev-parse", "--verify", f"{facts.prepared_ref}^{{commit}}"],
                )
                != facts.prepared_merge_commit
            ):
                raise ValueError("prepared integration Git authority changed before registration")
            for method_name in (
                "commit_reversible",
                "rollback",
                "abort",
                "finalize_reversible",
                "complete_publish",
                "retry_guard_release",
            ):
                if not callable(getattr(value, method_name, None)):
                    raise TypeError("prepared integration omitted a transaction method")
            registered_integration = value

        try:
            execution_admission = private_native_plan_admission(admission)
            self._assert_current_provider(execution_admission)
            fresh_plan = self._admission_revalidator(execution_admission)
            revalidate_pytest_executable_binding(
                execution_admission.pytest_executable,
                workspace_root=execution_admission.workspace_git.workspace_root,
            )
            if execution_deadline.is_exhausted():
                raise TimeoutError("run deadline exhausted during Host preflight")
            guarded_reload_count = 0

            def _reload_native_plan_under_guard() -> dict[str, object]:
                nonlocal guarded_reload_count
                guarded_reload_count += 1
                if guarded_reload_count != 1:
                    raise RuntimeError("guard-held native admission reload is one-shot")
                guarded_admission = private_native_plan_admission(admission)
                if execution_deadline.is_exhausted():
                    raise TimeoutError("run deadline exhausted before guard-held admission")
                self._assert_current_provider(guarded_admission)
                guarded_plan = self._admission_revalidator(guarded_admission)
                revalidate_pytest_executable_binding(
                    guarded_admission.pytest_executable,
                    workspace_root=guarded_admission.workspace_git.workspace_root,
                )
                return copy.deepcopy(guarded_plan)

            result = self._sequential_runner(
                HostSequentialRunRequest(
                    native_paths=admission.paths,
                    native_plan=MappingProxyType(copy.deepcopy(fresh_plan)),
                    native_admission=private_native_plan_admission(admission),
                    plan_ready=PlanReady.model_validate_json(
                        admission.plan_ready.model_dump_json(by_alias=True),
                        strict=True,
                    ),
                    host_run_id=host_run_id,
                    native_run_id=admission.native_run_id,
                    operation_id=request.operation_id,
                    base_branch=admission.workspace_git.base_branch,
                    base_head=admission.workspace_git.base_head,
                    pytest_executable=execution_admission.pytest_executable,
                    started_at=started_at,
                    execution_deadline=execution_deadline,
                    reload_native_plan_under_guard=_reload_native_plan_under_guard,
                    register_prepared_integration=_register_prepared_integration,
                ),
                progress_sink=streamed_progress.append,
            )
            if not isinstance(result, HostSequentialRunResult):
                raise TypeError("runner returned the wrong result contract")
            if result.prepared_integration is not None:
                if registered_integration is None:
                    raise RuntimeError("runner returned an unregistered prepared integration")
                if result.prepared_integration is not registered_integration:
                    raise RuntimeError("runner replaced its registered prepared integration")
            observed = self._roundtrip_payload(result.terminal, TerminalResult)
            assert isinstance(observed, TerminalResult)
            observed_terminal = observed
            prepared_integration = result.prepared_integration
            if prepared_integration is None:
                integration_commit_proof = self._validated_integration_commit_proof(
                    result.integration_commit_proof,
                    admission=admission,
                    terminal=observed,
                )
            else:
                if result.integration_commit_proof is not None:
                    raise ValueError("prepared runner result already carried a commit proof")
                integration_commit_proof = None
            if guarded_reload_count != 1:
                raise RuntimeError("runner skipped the guard-held native admission reload")
            progress = result.progress
            if streamed_progress and progress and tuple(streamed_progress) != progress:
                raise ValueError("runner progress result contradicts its streamed progress")
            if not progress:
                progress = tuple(streamed_progress)
            private_progress = self._validated_runner_progress(
                progress,
                admission,
            )
            private_evidence = self._validated_runner_evidence(result.evidence, admission)
            if prepared_integration is not None:
                # Validate a complete success draft while the approved base is
                # still untouched.  This closes every translation/codec/ledger
                # failure that previously occurred after an irreversible CAS.
                draft_elapsed_ms = self._successful_elapsed_ms(
                    admission=admission,
                    execution_deadline=execution_deadline,
                )
                draft_terminal = self._validated_runner_terminal(
                    result.terminal,
                    admission,
                    private_evidence,
                    started_at=started_at,
                    execution_deadline=execution_deadline,
                    integration_commit_proof=None,
                    successful_elapsed_ms=draft_elapsed_ms,
                )
                draft_events = self._build_run_events(
                    payloads=(
                        *private_progress,
                        *private_evidence,
                        RunTerminal(type="run.terminal", result=draft_terminal),
                    ),
                    operation_id=request.operation_id,
                    identity=run_identity,
                    sent_at=self._safe_now(started_at),
                )
                self._validate_run_event_batch(draft_events)

                integration_commit_proof = prepared_integration.commit_reversible()

                finalize_proof = prepared_integration.finalize_reversible()
                if finalize_proof.commit_proof != integration_commit_proof:
                    raise ValueError("integration finalization changed its commit authority")
                # External writers are outside the Forge mutation guard.  Read
                # and bind every evidence byte again after strict native
                # finalization and before terminal truth can be staged.
                private_evidence = self._validated_runner_evidence(result.evidence, admission)
                final_elapsed_ms = finalize_proof.final_elapsed_ms
                terminal = self._validated_runner_terminal(
                    result.terminal,
                    admission,
                    private_evidence,
                    started_at=started_at,
                    execution_deadline=execution_deadline,
                    integration_commit_proof=integration_commit_proof,
                    successful_elapsed_ms=final_elapsed_ms,
                )
                integration_commit_proof = self._validated_integration_commit_proof(
                    integration_commit_proof,
                    admission=admission,
                    terminal=terminal,
                )
                events = self._build_run_events(
                    payloads=(
                        *private_progress,
                        *private_evidence,
                        RunTerminal(type="run.terminal", result=terminal),
                    ),
                    operation_id=request.operation_id,
                    identity=run_identity,
                    sent_at=self._safe_now(started_at),
                )
                self._validate_run_event_batch(events)
                publication = self._prepare_run_publication(request=request, events=events)
                if self._validated_runner_evidence(result.evidence, admission) != private_evidence:
                    raise ValueError("retained evidence changed before authoritative publication")
                self._validated_integration_commit_proof(
                    integration_commit_proof,
                    admission=admission,
                    terminal=terminal,
                )

                # Publish only into the lock-held service image while the
                # mutation guard and rollback authority are still retained.
                # A seal failure restores the old image before the transaction
                # rolls Git/native state back, so no caller can observe a
                # succeeded Host terminal paired with an unsealed integration.
                previous_publication = self._snapshot_publication_state()
                try:
                    self._publish_run_publication(publication)
                    prepared_integration.complete_publish(
                        event_bytes_digest=self._publication_digest(
                            publication.outcome.event_bytes
                        ),
                        event_bytes=sum(len(value) for value in publication.outcome.event_bytes),
                    )
                except Exception:
                    self._restore_publication_state(previous_publication)
                    raise
                # A normally returned concrete transaction has crossed its
                # durable SEALED boundary.  No diagnostic/property failure
                # after that irreversible point may reclassify the already
                # published success.  Guard cleanup is advisory and retained
                # for retry without affecting terminal truth.
                try:
                    if prepared_integration.guard_release_pending:
                        released = prepared_integration.retry_guard_release()
                        if not released or prepared_integration.guard_release_pending:
                            self._pending_guard_releases.append(prepared_integration)
                except Exception:  # noqa: BLE001 - sealed success cannot be rolled back
                    try:
                        self._pending_guard_releases.append(prepared_integration)
                    except Exception:
                        pass
                return publication.outcome

            terminal = self._validated_runner_terminal(
                result.terminal,
                admission,
                private_evidence,
                started_at=started_at,
                execution_deadline=execution_deadline,
                integration_commit_proof=integration_commit_proof,
            )
            events = self._build_run_events(
                payloads=(
                    *private_progress,
                    *private_evidence,
                    RunTerminal(type="run.terminal", result=terminal),
                ),
                operation_id=request.operation_id,
                identity=run_identity,
                sent_at=self._safe_now(started_at),
            )
            self._validate_run_event_batch(events)
        except Exception:
            if registered_integration is not None:
                try:
                    if registered_integration.state == "PREPARED":
                        registered_integration.abort("Host validation failed before integration")
                    elif registered_integration.state in {
                        "COMMITTED_REVERSIBLE",
                        "PUBLISH_READY",
                    }:
                        registered_integration.rollback(
                            "Host validation failed before authoritative publish"
                        )
                except Exception:
                    rollback_failed = True
            finished_at = self._safe_now(started_at)
            if not private_progress and streamed_progress:
                private_progress = self._validated_progress_prefix(
                    streamed_progress,
                    admission,
                )
            fallback_evidence, fallback_terminal = self._failed_result(
                admission=admission,
                started_at=started_at,
                finished_at=finished_at,
                execution_deadline=execution_deadline,
                observed_terminal=observed_terminal,
                progress=private_progress,
            )
            if rollback_failed or (
                registered_integration is not None
                and registered_integration.state == "ROLLBACK_FAILED"
            ):
                fallback_payload = fallback_terminal.model_dump(mode="python", by_alias=False)
                fallback_payload.update(
                    status="interrupted",
                    reason=TerminalReason(
                        code="integration_rollback_failed",
                        message=(
                            "Forge could not prove that the reversible integration was restored; "
                            "manual workspace inspection is required."
                        ),
                        retryable=False,
                    ),
                    exit_code=2,
                )
                fallback_terminal = TerminalResult.model_validate(
                    fallback_payload,
                    strict=True,
                )
            events = self._build_run_events(
                payloads=(
                    *private_progress,
                    fallback_evidence,
                    RunTerminal(type="run.terminal", result=fallback_terminal),
                ),
                operation_id=request.operation_id,
                identity=run_identity,
                sent_at=finished_at,
            )
            self._validate_run_event_batch(events)

        publication = self._prepare_run_publication(request=request, events=events)
        self._publish_run_publication(publication)
        return publication.outcome

    def _build_run_events(
        self,
        *,
        payloads: tuple[object, ...],
        operation_id: str,
        identity: Identity,
        sent_at: datetime,
    ) -> tuple[EventEnvelope, ...]:
        sequence = self._ledger.last_sequence + 1
        return tuple(
            self._event(
                payload=payload,
                operation_id=operation_id,
                identity=identity,
                sequence=sequence + offset,
                sent_at=sent_at,
            )
            for offset, payload in enumerate(payloads)
        )

    def _validate_run_event_batch(self, events: tuple[EventEnvelope, ...]) -> None:
        if not events or not isinstance(events[-1].payload, RunTerminal):
            raise ValueError("a run batch must end in exactly one terminal event")
        if sum(isinstance(event.payload, RunTerminal) for event in events) != 1:
            raise ValueError("a run batch must contain exactly one terminal event")
        shadow = copy.deepcopy(self._ledger)
        for event in events:
            shadow.accept_event(self._private_event(event))

    @staticmethod
    def _roundtrip_payload(payload: object, expected_type: type) -> object:
        if not isinstance(payload, expected_type):
            raise TypeError("runner returned the wrong typed payload")
        return expected_type.model_validate_json(  # type: ignore[attr-defined,no-any-return]
            payload.model_dump_json(by_alias=True),  # type: ignore[attr-defined]
            strict=True,
        )

    def _validated_runner_evidence(
        self,
        evidence: tuple[EvidenceReady, ...],
        admission: NativePlanAdmission,
    ) -> tuple[EvidenceReady, ...]:
        private = tuple(self._roundtrip_payload(item, EvidenceReady) for item in evidence)
        if not private:
            raise ValueError("runner returned no evidence.ready payload")
        for ready in private:
            assert isinstance(ready, EvidenceReady)
            for artifact in ready.artifacts:
                self._validate_artifact(artifact, admission.paths.root)
        return private  # type: ignore[return-value]

    def _validated_runner_progress(
        self,
        progress: Sequence[HostSequentialProgress],
        admission: NativePlanAdmission,
    ) -> tuple[RunProgress, ...]:
        private: list[RunProgress] = []
        expected_task_ids = {task.task_id for task in admission.plan_ready.plan_definition.tasks}
        previous_steps: dict[str, int] = {}
        max_steps = admission.plan_ready.execution_admission.per_managed_task_max_steps
        for item in progress:
            if not isinstance(item, HostSequentialProgress):
                raise TypeError("runner returned the wrong progress contract")
            payload = self._roundtrip_payload(item.to_payload(), RunProgress)
            assert isinstance(payload, RunProgress)
            if payload.task_id is not None:
                if payload.task_id not in expected_task_ids:
                    raise ValueError("runner progress references an unmanaged task")
                assert payload.task_model_steps is not None
                if payload.task_model_steps > max_steps:
                    raise ValueError("runner progress exceeds the admitted task budget")
                if payload.task_model_steps < previous_steps.get(payload.task_id, 0):
                    raise ValueError("runner progress task model steps regressed")
                previous_steps[payload.task_id] = payload.task_model_steps
            private.append(payload)
            worker_ids = {value.worker_id for value in private if value.worker_id is not None}
            worker_cap = min(
                admission.plan_ready.execution_admission.execution_strategy.max_parallel_workers,
                len(expected_task_ids),
            )
            if len(worker_ids) > worker_cap:
                raise ValueError("runner progress exceeds the admitted worker allocation")
        return tuple(private)

    def _validated_progress_prefix(
        self,
        progress: Sequence[HostSequentialProgress],
        admission: NativePlanAdmission,
    ) -> tuple[RunProgress, ...]:
        """Retain the longest canonical causal prefix after a runner exception."""

        accepted: list[HostSequentialProgress] = []
        last_valid: tuple[RunProgress, ...] = ()
        for item in progress:
            accepted.append(item)
            try:
                last_valid = self._validated_runner_progress(accepted, admission)
            except Exception:  # noqa: BLE001 - untrusted runner conversion boundary
                break
        return last_valid

    @staticmethod
    def _validate_artifact(artifact: ArtifactRecord, root: Path) -> None:
        if artifact.status not in {"created", "updated", "unchanged"}:
            return
        candidate = root.joinpath(*artifact.workspace_relative_path.split("/"))
        try:
            mode = os.lstat(candidate).st_mode
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise ValueError("runner artifact does not resolve beneath the workspace") from exc
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise ValueError("Phase-0 retained evidence must be a regular non-symlink file")
        data = resolved.read_bytes()
        if hashlib.sha256(data).hexdigest() != artifact.digest or len(data) != artifact.size_bytes:
            raise ValueError("runner artifact digest or size does not match disk")

    @staticmethod
    def _git_output(root: Path, args: list[str]) -> str:
        try:
            completed = subprocess.run(
                build_git_cmd(root, args),
                check=False,
                capture_output=True,
                **command_text(),
                env=build_git_process_env(),
                timeout=5.0,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("integration proof Git validation was unavailable") from exc
        if completed.returncode != 0:
            raise ValueError("integration proof Git validation failed")
        return completed.stdout.strip()

    def _validated_integration_commit_proof(
        self,
        proof: HostIntegrationCommitProof | None,
        *,
        admission: NativePlanAdmission,
        terminal: TerminalResult,
    ) -> HostIntegrationCommitProof | None:
        if terminal.status != "succeeded":
            if proof is not None:
                raise ValueError("a non-success terminal cannot carry an integration proof")
            return None
        if not isinstance(proof, HostIntegrationCommitProof):
            raise ValueError("a successful run requires an integration commit proof")
        private = HostIntegrationCommitProof(
            base_branch=str(proof.base_branch),
            approved_base_head=str(proof.approved_base_head),
            task_commit=str(proof.task_commit),
            merge_commit=str(proof.merge_commit),
            integration_committed_elapsed_ms=proof.integration_committed_elapsed_ms,
        )
        if (
            private.base_branch != admission.workspace_git.base_branch
            or private.approved_base_head != admission.workspace_git.base_head
        ):
            raise ValueError("integration proof does not match the approved Git base")
        hashes = (
            private.approved_base_head,
            private.task_commit,
            private.merge_commit,
        )
        if any(
            len(value) not in {40, 64}
            or value != value.lower()
            or any(character not in "0123456789abcdef" for character in value)
            for value in hashes
        ):
            raise ValueError("integration proof contains an invalid commit identity")
        timeout_ms = admission.plan_ready.execution_admission.run_timeout_ms
        if (
            isinstance(private.integration_committed_elapsed_ms, bool)
            or not isinstance(private.integration_committed_elapsed_ms, int)
            or private.integration_committed_elapsed_ms < 0
            or private.integration_committed_elapsed_ms >= timeout_ms
        ):
            raise ValueError("integration proof crossed the admitted deadline")

        root = admission.workspace_git.git_root
        base_ref = self._git_output(
            root,
            ["rev-parse", "--verify", f"refs/heads/{private.base_branch}^{{commit}}"],
        )
        if base_ref != private.merge_commit:
            raise ValueError("approved base ref does not point at the proven merge commit")
        parents = self._git_output(
            root,
            ["rev-list", "--parents", "-n", "1", private.merge_commit],
        ).split()
        if parents != [
            private.merge_commit,
            private.approved_base_head,
            private.task_commit,
        ]:
            raise ValueError("integration proof merge parents do not match exact authority")
        if terminal.timing.elapsed_ms < private.integration_committed_elapsed_ms:
            raise ValueError("terminal timing predates the integration commit point")
        return private

    def _validated_runner_terminal(
        self,
        terminal: TerminalResult,
        admission: NativePlanAdmission,
        evidence_payloads: tuple[EvidenceReady, ...],
        *,
        started_at: datetime,
        execution_deadline: ExecutionDeadline,
        integration_commit_proof: HostIntegrationCommitProof | None,
        successful_elapsed_ms: int | None = None,
    ) -> TerminalResult:
        private = self._roundtrip_payload(terminal, TerminalResult)
        assert isinstance(private, TerminalResult)
        ready = admission.plan_ready
        if (
            private.host_plan_fingerprint != ready.host_plan_fingerprint
            or private.native_approval_fingerprint != ready.native_approval_fingerprint
            or private.verification_contract_fingerprint
            != ready.verification_contract.contract_fingerprint
            or private.execution_admission_fingerprint != ready.execution_admission.fingerprint
        ):
            raise ValueError("runner terminal fingerprints do not match exact admission")
        contract_checks = {check.check_id: check for check in ready.verification_contract.checks}
        result_checks = {
            check.check_id: ("acceptance", check) for check in private.acceptance.checks
        }
        result_checks.update(
            {check.check_id: ("verification", check) for check in private.verification.checks}
        )
        if set(result_checks) != set(contract_checks):
            raise ValueError("runner terminal does not cover the exact approved checks")
        if any(
            result_checks[check_id][0] != definition.domain
            for check_id, definition in contract_checks.items()
        ):
            raise ValueError("runner terminal check domains do not match the contract")
        evidence_by_id = {
            record.evidence_id: record
            for payload in evidence_payloads
            for record in payload.evidence
        }
        command_by_id = {
            command.command_id: command for command in ready.verification_contract.commands
        }
        for domain, aggregate in (
            ("acceptance", private.acceptance),
            ("verification", private.verification),
        ):
            for check in aggregate.checks:
                records = [evidence_by_id.get(item) for item in check.evidence_refs]
                if any(record is None for record in records):
                    raise ValueError("runner terminal references unknown evidence")
                if check.status == "passed" and (
                    not records or any(record.status != "passed" for record in records if record)
                ):
                    raise ValueError("passed checks require passed evidence")
                if domain == "verification" and check.status == "passed":
                    for record in records:
                        assert record is not None
                        if not record.real_execution or record.timed_out or record.exit_code != 0:
                            raise ValueError(
                                "passed verification is not a real successful execution"
                            )
                        if any(
                            command_by_id[command_id].evidentiary_capability != "ASSERTIVE"
                            for command_id in record.command_ids
                        ):
                            raise ValueError("passed verification used a non-assertive command")
        terminal_payload = private.model_dump(mode="python", by_alias=False)
        if private.status == "succeeded":
            if integration_commit_proof is None and successful_elapsed_ms is None:
                raise ValueError("successful terminal omitted Host timing authority")
            elapsed_ms = (
                successful_elapsed_ms
                if successful_elapsed_ms is not None
                else integration_commit_proof.integration_committed_elapsed_ms
            )
            assert elapsed_ms is not None
            timeout_ms = ready.execution_admission.run_timeout_ms
            if elapsed_ms < 0 or elapsed_ms >= timeout_ms:
                raise ValueError("successful terminal exceeded the admitted deadline")
            terminal_payload["timing"] = Timing(
                started_at=started_at,
                finished_at=self._safe_now(started_at),
                elapsed_ms=elapsed_ms,
                timed_out=False,
            )
        else:
            terminal_payload["timing"] = self._host_timing(
                admission=admission,
                started_at=started_at,
                finished_at=self._safe_now(started_at),
                execution_deadline=execution_deadline,
            )
        # Revalidation after replacing untrusted engine timing prevents a
        # runner from succeeding after the Host-owned deadline has expired.
        return TerminalResult.model_validate(terminal_payload, strict=True)

    @staticmethod
    def _host_timing(
        *,
        admission: NativePlanAdmission,
        started_at: datetime,
        finished_at: datetime,
        execution_deadline: ExecutionDeadline,
    ) -> Timing:
        try:
            elapsed_ms = max(0, round(execution_deadline.elapsed_seconds() * 1000))
            timed_out = (
                execution_deadline.is_exhausted()
                or elapsed_ms >= admission.plan_ready.execution_admission.run_timeout_ms
            )
        except Exception:  # noqa: BLE001 - post-commit monotonic failure is terminal
            # The deadline clock is authority, not optional telemetry.  If it
            # becomes unavailable after run.start commits, fail closed as a
            # timeout while still producing one terminal and replay record.
            elapsed_ms = admission.plan_ready.execution_admission.run_timeout_ms
            timed_out = True
        return Timing(
            started_at=started_at,
            finished_at=max(finished_at, started_at),
            elapsed_ms=elapsed_ms,
            timed_out=timed_out,
        )

    @staticmethod
    def _successful_elapsed_ms(
        *,
        admission: NativePlanAdmission,
        execution_deadline: ExecutionDeadline,
    ) -> int:
        """Sample whole-run monotonic authority for a still-reversible success."""

        elapsed_ms = max(0, round(execution_deadline.elapsed_seconds() * 1000))
        timeout_ms = admission.plan_ready.execution_admission.run_timeout_ms
        if execution_deadline.is_exhausted() or elapsed_ms >= timeout_ms:
            raise TimeoutError("run deadline exhausted before authoritative publication")
        return elapsed_ms

    def _failed_result(
        self,
        *,
        admission: NativePlanAdmission,
        started_at: datetime,
        finished_at: datetime,
        execution_deadline: ExecutionDeadline,
        observed_terminal: TerminalResult | None,
        progress: tuple[RunProgress, ...],
    ) -> tuple[EvidenceReady, TerminalResult]:
        ready = admission.plan_ready
        evidence: list[EvidenceRecord] = []
        acceptance_checks: list[CheckResult] = []
        verification_checks: list[CheckResult] = []
        for definition in ready.verification_contract.checks:
            evidence_id = self._new_id("evidence")
            command_ids = list(definition.command_ids)
            record = EvidenceRecord(
                evidence_id=evidence_id,
                role=definition.domain,
                kind="test" if definition.domain == "verification" else "review",
                status="not_run",
                check_id=definition.check_id,
                task_id=definition.task_id,
                requirement_ids=list(definition.requirement_ids),
                command_ids=command_ids,
                real_execution=False if definition.domain == "verification" else None,
                exit_code=None,
                timed_out=False if definition.domain == "verification" else None,
                summary="The engine did not produce admissible evidence for this check.",
                artifact_refs=[],
            )
            evidence.append(record)
            check = CheckResult(
                check_id=definition.check_id,
                task_id=definition.task_id,
                requirement_ids=list(definition.requirement_ids),
                status="not_run",
                summary="The check was not completed with admissible evidence.",
                evidence_refs=[evidence_id],
            )
            if definition.domain == "acceptance":
                acceptance_checks.append(check)
            else:
                verification_checks.append(check)
        evidence_ready = EvidenceReady(type="evidence.ready", evidence=evidence, artifacts=[])
        task_ids = [task.task_id for task in ready.plan_definition.tasks]
        progress_steps = {
            item.task_id: item.task_model_steps
            for item in progress
            if item.task_id is not None and item.task_model_steps is not None
        }
        task_usage = [
            TaskExecutionUsage(task_id=task_id, model_steps=progress_steps.get(task_id, 0))
            for task_id in task_ids
        ]
        usage = Usage(
            model_calls=sum(item.model_steps for item in task_usage),
            input_tokens=0,
            output_tokens=0,
            cost_known=False,
            total_cost_micros=None,
        )
        timing = self._host_timing(
            admission=admission,
            started_at=started_at,
            finished_at=finished_at,
            execution_deadline=execution_deadline,
        )
        observed_worker_ids = {item.worker_id for item in progress if item.worker_id is not None}
        workers = Workers(
            requested=len(observed_worker_ids),
            started=len(observed_worker_ids),
            succeeded=0,
            failed=len(observed_worker_ids),
        )
        if observed_terminal is not None:
            observed_by_task = {
                item.task_id: item.model_steps for item in observed_terminal.task_usage
            }
            if set(observed_by_task) == set(task_ids) and all(
                observed_by_task[task_id] == progress_steps.get(task_id, 0) for task_id in task_ids
            ):
                task_usage = [
                    TaskExecutionUsage(
                        task_id=task_id,
                        model_steps=observed_by_task[task_id],
                    )
                    for task_id in task_ids
                ]
            if observed_terminal.usage.model_calls >= sum(item.model_steps for item in task_usage):
                usage = Usage.model_validate_json(
                    observed_terminal.usage.model_dump_json(by_alias=True),
                    strict=True,
                )
            worker_cap = min(
                ready.execution_admission.execution_strategy.max_parallel_workers,
                len(task_ids),
            )
            if (
                observed_terminal.workers.requested <= worker_cap
                and observed_terminal.workers.started >= len(observed_worker_ids)
            ):
                workers = Workers.model_validate_json(
                    observed_terminal.workers.model_dump_json(by_alias=True),
                    strict=True,
                )
        terminal = TerminalResult(
            host_plan_fingerprint=ready.host_plan_fingerprint,
            native_approval_fingerprint=ready.native_approval_fingerprint,
            verification_contract_fingerprint=ready.verification_contract.contract_fingerprint,
            execution_admission_fingerprint=ready.execution_admission.fingerprint,
            status="failed",
            reason=TerminalReason(
                code="engine_result_invalid",
                message="The engine did not return admissible terminal evidence.",
                retryable=False,
            ),
            exit_code=1,
            acceptance=VerificationResult(status="not_run", checks=acceptance_checks),
            verification=VerificationResult(status="not_run", checks=verification_checks),
            usage=usage,
            timing=timing,
            workers=workers,
            task_usage=task_usage,
            artifact_refs=[],
        )
        return evidence_ready, terminal

    def replay(self, *, after_sequence: int = 0) -> tuple[bytes, ...]:
        """Return byte-identical event replay; Phase 0 supports only cursor zero."""

        with self._lock:
            if after_sequence != 0:
                raise ReplayUnavailableError(
                    "Phase-0 replay requires afterSequence=0 without a durable checkpoint"
                )
            return tuple(self._event_bytes)


__all__ = [
    "AdmissionRevalidator",
    "ForgeHostPlannerAdapter",
    "ForgeHostPlannerAdapterSettings",
    "ForgeHostService",
    "HostIntegrationCommitProof",
    "HostIntegrationFinalizeProof",
    "HostPreparedIntegration",
    "HostPreparedIntegrationFacts",
    "HostPlanner",
    "HostPlannerOutcome",
    "HostPlannerRequest",
    "HostSequentialProgress",
    "HostSequentialRunRequest",
    "HostSequentialRunResult",
    "HostSequentialRunner",
    "HostServiceOutcome",
]
