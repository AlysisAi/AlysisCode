"""Thin semantic comparison for validated Forge Host Protocol v2 traces.

This module is deliberately downstream of :mod:`forge_host_protocol`. It does
not define a second wire schema, validate plans, normalize paths, or translate
CLI/App/VS Code formats. Every supplied model is revalidated through the v2 wire
boundary. Callers must also apply :class:`ProtocolLedger` to the complete stream
before selecting its conformance-bearing messages.

No real CLI, Desktop App, or VS Code capture serializer is implemented here.
Consequently this utility does **not** establish cross-surface parity and must
not yet be used as a release gate.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, TypeAlias

from .forge_host_protocol import (
    ApprovalDecision,
    ApprovalRequired,
    EventEnvelope,
    EvidenceReady,
    ForgeHostProtocolError,
    PlanReady,
    RequestEnvelope,
    RunCancel,
    RunProgress,
    RunStart,
    RunTerminal,
    to_wire_dict,
    validate_envelope,
)

SURFACE_CAPTURE_ADAPTERS: tuple[str, ...] = ()
"""Real CLI/App/VS Code canonical capture adapters implemented today: none."""

SURFACE_PARITY_STATUS = "not_evaluated"
"""Cross-surface parity cannot be claimed until real serializers are captured."""

KNOWN_PROTOCOL_GAPS: tuple[str, ...] = (
    "Forge Host v2 has no dedicated recovery/checkpoint event, so recovery semantics "
    "cannot yet be compared.",
    "Forge Host v2 has no canonical preview telemetry message.",
    "Forge Host v2 has no canonical Computer Use telemetry message.",
)

RecordIdentityPart: TypeAlias = str | int | None | tuple[str, ...]
RecordIdentity: TypeAlias = tuple[RecordIdentityPart, ...]
AuthorityIdentity: TypeAlias = tuple[str, str, str | None, int]
MismatchPathPart: TypeAlias = str | int
MismatchPath: TypeAlias = tuple[MismatchPathPart, ...]

_MISSING_TEXT = "<missing>"

_ConformancePayload: TypeAlias = (
    PlanReady
    | RunStart
    | ApprovalRequired
    | ApprovalDecision
    | RunProgress
    | EvidenceReady
    | RunTerminal
    | RunCancel
)
_CONFORMANCE_PAYLOAD_TYPES = (
    PlanReady,
    RunStart,
    ApprovalRequired,
    ApprovalDecision,
    RunProgress,
    EvidenceReady,
    RunTerminal,
    RunCancel,
)


class ForgeConformanceInputError(ValueError):
    """Raised when input cannot be revalidated as a canonical conformance trace."""


@dataclass(frozen=True, slots=True)
class _RecordView:
    message_type: str
    identity: RecordIdentity
    authority: AuthorityIdentity
    operation_id: str | None
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True, init=False)
class _ForgeHostTraceRecord:
    """Private, wire-backed record with no caller-accessible constructor."""

    _envelope_json: str

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("Forge Host trace records are created only by build_forge_host_trace")

    @classmethod
    def _create(cls, envelope_json: str) -> _ForgeHostTraceRecord:
        record = object.__new__(cls)
        object.__setattr__(record, "_envelope_json", envelope_json)
        return record

    @property
    def message_type(self) -> str:
        return _record_view(self).message_type

    @property
    def identity(self) -> RecordIdentity:
        return _record_view(self).identity

    @property
    def authority(self) -> AuthorityIdentity:
        return _record_view(self).authority

    @property
    def operation_id(self) -> str | None:
        return _record_view(self).operation_id

    def payload_dict(self) -> dict[str, Any]:
        """Return a fresh, canonical-time copy of the validated payload."""

        return _record_view(self).payload


@dataclass(frozen=True, slots=True, init=False)
class _ForgeHostTrace:
    """Private ordered trace with no caller-accessible constructor."""

    label: str
    records: tuple[_ForgeHostTraceRecord, ...]

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("Forge Host traces are created only by build_forge_host_trace")

    @classmethod
    def _create(
        cls,
        label: str,
        records: tuple[_ForgeHostTraceRecord, ...],
    ) -> _ForgeHostTrace:
        trace = object.__new__(cls)
        object.__setattr__(trace, "label", label)
        object.__setattr__(trace, "records", records)
        return trace


@dataclass(frozen=True, slots=True)
class ForgeTraceMismatch:
    """One structured difference; paths and identities are collision-safe tuples."""

    record_index: int
    expected_identity: RecordIdentity | None
    actual_identity: RecordIdentity | None
    path: MismatchPath
    kind: str
    expected: Any
    actual: Any

    def location(self) -> str:
        location = f"records[{self.record_index}]"
        for component in self.path:
            location += f"[{component}]" if isinstance(component, int) else f".{component}"
        return location

    def __str__(self) -> str:
        expected = _MISSING_TEXT if self.kind == "unexpected" else repr(self.expected)
        actual = _MISSING_TEXT if self.kind == "missing" else repr(self.actual)
        return f"{self.location()}: expected {expected}, actual {actual}"


class ForgeTraceConformanceError(AssertionError):
    """Raised when two canonical v2 traces have different semantics."""

    def __init__(
        self,
        expected_label: str,
        actual_label: str,
        mismatches: tuple[ForgeTraceMismatch, ...],
    ) -> None:
        self.expected_label = expected_label
        self.actual_label = actual_label
        self.mismatches = mismatches
        details = "\n".join(f"- {mismatch}" for mismatch in mismatches)
        super().__init__(
            "Forge Host v2 trace conformance failed "
            f"({expected_label} != {actual_label}; {len(mismatches)} mismatch(es)):\n"
            f"{details}"
        )


def build_forge_host_trace(
    envelopes: Iterable[RequestEnvelope | EventEnvelope],
    *,
    label: str = "canonical",
) -> _ForgeHostTrace:
    """Build a trace from v2 request/event models after strict wire revalidation.

    Envelope message IDs, send times, event sequence numbers, and request retry
    metadata are transport-only and are omitted. Correlation ``operationId`` and
    authoritative session, generation, run, and revision identity are retained
    for every record. The validated payload is kept in canonical wire shape;
    arrays remain ordered and workspace-relative paths or opaque references are
    never rewritten.
    """

    if not isinstance(label, str) or not label.strip():
        raise ForgeConformanceInputError("trace label must be a non-empty string")

    records: list[_ForgeHostTraceRecord] = []
    for index, supplied in enumerate(envelopes):
        if not isinstance(supplied, (RequestEnvelope, EventEnvelope)):
            raise ForgeConformanceInputError(
                f"envelopes[{index}] must be a RequestEnvelope or EventEnvelope model; "
                "raw mappings and response envelopes are not accepted"
            )
        try:
            validated = validate_envelope(to_wire_dict(supplied))
        except ForgeHostProtocolError as exc:
            raise ForgeConformanceInputError(
                f"envelopes[{index}] failed Forge Host v2 wire revalidation: {exc}"
            ) from exc
        if not isinstance(validated, (RequestEnvelope, EventEnvelope)):
            raise ForgeConformanceInputError(
                f"envelopes[{index}] is not a request/event conformance envelope"
            )
        if not isinstance(validated.payload, _CONFORMANCE_PAYLOAD_TYPES):
            payload_type = getattr(validated.payload, "type", type(validated.payload).__name__)
            raise ForgeConformanceInputError(
                f"envelopes[{index}] payload {payload_type!r} is outside the focused "
                "run-conformance trace"
            )
        envelope_json = json.dumps(
            to_wire_dict(validated),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        records.append(_ForgeHostTraceRecord._create(envelope_json))
    return _ForgeHostTrace._create(label.strip(), tuple(records))


def compare_forge_host_traces(
    expected: _ForgeHostTrace,
    actual: _ForgeHostTrace,
) -> tuple[ForgeTraceMismatch, ...]:
    """Return deterministic differences after revalidating both private traces."""

    expected_views = _trace_views(expected)
    actual_views = _trace_views(actual)
    mismatches: list[ForgeTraceMismatch] = []
    common = min(len(expected_views), len(actual_views))
    for index in range(common):
        expected_view = expected_views[index]
        actual_view = actual_views[index]
        if expected_view.operation_id != actual_view.operation_id:
            mismatches.append(
                ForgeTraceMismatch(
                    index,
                    expected_view.identity,
                    actual_view.identity,
                    ("operationId",),
                    "value",
                    expected_view.operation_id,
                    actual_view.operation_id,
                )
            )
        if expected_view.authority != actual_view.authority:
            mismatches.append(
                ForgeTraceMismatch(
                    index,
                    expected_view.identity,
                    actual_view.identity,
                    ("authority",),
                    "value",
                    expected_view.authority,
                    actual_view.authority,
                )
            )
        if expected_view.identity != actual_view.identity:
            mismatches.append(
                ForgeTraceMismatch(
                    index,
                    expected_view.identity,
                    actual_view.identity,
                    ("identity",),
                    "value",
                    expected_view.identity,
                    actual_view.identity,
                )
            )
        if expected_view.message_type != actual_view.message_type:
            mismatches.append(
                ForgeTraceMismatch(
                    index,
                    expected_view.identity,
                    actual_view.identity,
                    ("messageType",),
                    "value",
                    expected_view.message_type,
                    actual_view.message_type,
                )
            )
            continue
        _compare_values(
            expected_view.payload,
            actual_view.payload,
            index,
            expected_view.identity,
            actual_view.identity,
            ("payload",),
            mismatches,
        )

    for index in range(common, len(expected_views)):
        view = expected_views[index]
        mismatches.append(
            ForgeTraceMismatch(
                index, view.identity, None, (), "missing", view.message_type, _MISSING_TEXT
            )
        )
    for index in range(common, len(actual_views)):
        view = actual_views[index]
        mismatches.append(
            ForgeTraceMismatch(
                index, None, view.identity, (), "unexpected", _MISSING_TEXT, view.message_type
            )
        )
    return tuple(mismatches)


def assert_forge_host_traces_equal(
    expected: _ForgeHostTrace,
    actual: _ForgeHostTrace,
) -> None:
    """Assert exact semantic equality of two validated canonical v2 traces."""

    mismatches = compare_forge_host_traces(expected, actual)
    if mismatches:
        raise ForgeTraceConformanceError(expected.label, actual.label, mismatches)


def _trace_views(trace: _ForgeHostTrace) -> tuple[_RecordView, ...]:
    if type(trace) is not _ForgeHostTrace:
        raise ForgeConformanceInputError(
            "compare_forge_host_traces requires traces built by build_forge_host_trace"
        )
    if not isinstance(trace.label, str) or not trace.label or not isinstance(trace.records, tuple):
        raise ForgeConformanceInputError("Forge Host trace storage is invalid")
    views: list[_RecordView] = []
    for index, record in enumerate(trace.records):
        if type(record) is not _ForgeHostTraceRecord:
            raise ForgeConformanceInputError(f"trace.records[{index}] is not a built record")
        try:
            views.append(_record_view(record))
        except ForgeHostProtocolError as exc:
            raise ForgeConformanceInputError(
                f"trace.records[{index}] failed Forge Host v2 revalidation: {exc}"
            ) from exc
    return tuple(views)


def _record_view(record: _ForgeHostTraceRecord) -> _RecordView:
    envelope = validate_envelope(record._envelope_json)
    if not isinstance(envelope, (RequestEnvelope, EventEnvelope)) or not isinstance(
        envelope.payload, _CONFORMANCE_PAYLOAD_TYPES
    ):
        raise ForgeConformanceInputError("stored record is not a conformance envelope")
    payload = envelope.payload
    authority = envelope.identity if isinstance(envelope, EventEnvelope) else payload.identity
    authority_tuple: AuthorityIdentity = (
        authority.session_id,
        authority.generation_id,
        authority.run_id,
        authority.revision,
    )
    canonical_payload = _canonical_value(
        payload.model_dump(mode="python", by_alias=True, exclude_none=False)
    )
    if not isinstance(canonical_payload, dict):
        raise AssertionError("a Forge Host payload must be an object")
    return _RecordView(
        payload.type,
        _record_identity(payload),
        authority_tuple,
        envelope.operation_id,
        canonical_payload,
    )


def _record_identity(payload: _ConformancePayload) -> RecordIdentity:
    if isinstance(payload, PlanReady):
        return (
            payload.type,
            payload.host_plan_fingerprint,
            payload.native_approval_fingerprint,
            payload.verification_contract.contract_fingerprint,
            payload.execution_admission.fingerprint,
            payload.approval_policy.fingerprint,
        )
    if isinstance(payload, RunStart):
        return (
            payload.type,
            payload.host_plan_fingerprint,
            payload.native_approval_fingerprint,
            payload.verification_contract_fingerprint,
            payload.execution_admission.fingerprint,
        )
    if isinstance(payload, ApprovalRequired):
        return (
            payload.type,
            payload.approval_id,
            payload.action_fingerprint,
            payload.scope.kind,
            payload.scope.fingerprint,
        )
    if isinstance(payload, ApprovalDecision):
        return (
            payload.type,
            payload.approval_operation_id,
            payload.approval_id,
            payload.action_fingerprint,
            payload.scope_fingerprint,
        )
    if isinstance(payload, RunProgress):
        return (payload.type, payload.phase, payload.step, payload.worker_id)
    if isinstance(payload, EvidenceReady):
        return (
            payload.type,
            tuple(record.evidence_id for record in payload.evidence),
            tuple(record.artifact_id for record in payload.artifacts),
        )
    if isinstance(payload, RunTerminal):
        return (
            payload.type,
            payload.result.host_plan_fingerprint,
            payload.result.native_approval_fingerprint,
            payload.result.verification_contract_fingerprint,
            payload.result.execution_admission_fingerprint,
        )
    if isinstance(payload, RunCancel):
        return (payload.type, payload.reason_code)
    raise AssertionError(f"unsupported conformance payload: {type(payload).__name__}")


def _canonical_value(value: Any) -> Any:
    if isinstance(value, datetime):
        utc = value.astimezone(timezone.utc)
        timespec = "microseconds" if utc.microsecond else "seconds"
        return utc.isoformat(timespec=timespec).replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {key: _canonical_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    return value


def _compare_values(
    expected: Any,
    actual: Any,
    record_index: int,
    expected_identity: RecordIdentity,
    actual_identity: RecordIdentity,
    path: MismatchPath,
    mismatches: list[ForgeTraceMismatch],
) -> None:
    if type(expected) is not type(actual):
        mismatches.append(
            ForgeTraceMismatch(
                record_index,
                expected_identity,
                actual_identity,
                path,
                "type",
                expected,
                actual,
            )
        )
        return
    if isinstance(expected, Mapping):
        expected_keys = set(expected)
        actual_keys = set(actual)
        for key in sorted(expected_keys - actual_keys):
            mismatches.append(
                ForgeTraceMismatch(
                    record_index,
                    expected_identity,
                    actual_identity,
                    (*path, key),
                    "missing",
                    expected[key],
                    _MISSING_TEXT,
                )
            )
        for key in sorted(actual_keys - expected_keys):
            mismatches.append(
                ForgeTraceMismatch(
                    record_index,
                    expected_identity,
                    actual_identity,
                    (*path, key),
                    "unexpected",
                    _MISSING_TEXT,
                    actual[key],
                )
            )
        for key in sorted(expected_keys & actual_keys):
            _compare_values(
                expected[key],
                actual[key],
                record_index,
                expected_identity,
                actual_identity,
                (*path, key),
                mismatches,
            )
        return
    if isinstance(expected, list):
        for index, (expected_item, actual_item) in enumerate(zip(expected, actual, strict=False)):
            _compare_values(
                expected_item,
                actual_item,
                record_index,
                expected_identity,
                actual_identity,
                (*path, index),
                mismatches,
            )
        if len(expected) != len(actual):
            mismatches.append(
                ForgeTraceMismatch(
                    record_index,
                    expected_identity,
                    actual_identity,
                    path,
                    "length",
                    len(expected),
                    len(actual),
                )
            )
        return
    if expected != actual:
        mismatches.append(
            ForgeTraceMismatch(
                record_index,
                expected_identity,
                actual_identity,
                path,
                "value",
                expected,
                actual,
            )
        )
