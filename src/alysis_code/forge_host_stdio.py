"""Bounded JSONL transport for the canonical Forge Host-v2 service.

The transport owns framing and handshake negotiation only. Planner, approval,
execution, evidence, replay, and terminal semantics remain exclusively inside
``ForgeHostService``. CLI, Electron, and VS Code can therefore consume the same
wire stream without embedding a second controller.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import BinaryIO, Protocol

from .forge_host_protocol.codec import encode_envelope, validate_envelope
from .forge_host_protocol.errors import (
    ApprovalStateError,
    ForgeHostProtocolError,
    IdempotencyConflictError,
    PlanStateError,
    ProtocolValidationError,
    ReplayUnavailableError,
    StaleIdentityError,
    StaleRevisionError,
    UnsafeSerializationError,
    UnsupportedVersionError,
)
from .forge_host_protocol.models import (
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    ApprovalDecision,
    ApprovalRequired,
    Capability,
    EventEnvelope,
    HandshakeRequest,
    Identity,
    Peer,
    PlannerInput,
    ProtocolError,
    RequestEnvelope,
    ResponseEnvelope,
    RunCancel,
    RunStart,
)
from .forge_host_protocol.negotiation import negotiate_handshake
from .forge_host_service import HostServiceOutcome

MAX_WIRE_LINE_BYTES = 4 * 1024 * 1024

DEFAULT_HOST_CAPABILITIES: tuple[Capability, ...] = (
    Capability(name="forge.approval", version=1),
    Capability(name="forge.evidence", version=1),
    Capability(name="forge.execute", version=1),
    Capability(name="forge.plan", version=1),
)

_ERROR_SUMMARIES = {
    "invalid_message": "The message is invalid.",
    "unsupported_version": "The protocol version is unsupported.",
    "capability_unavailable": "The requested capability is unavailable.",
    "stale_identity": "The message identity is stale.",
    "stale_revision": "The message revision is stale.",
    "idempotency_conflict": "The idempotency authority conflicts.",
    "invalid_transition": "The protocol transition is invalid.",
    "internal_error": "The protocol operation failed.",
}


class ForgeHostStdioFatalError(RuntimeError):
    """Framing failed before a safe correlated response could be emitted."""


class _CapabilityUnavailableError(ForgeHostProtocolError):
    """A correlated command requires a capability the peer did not negotiate."""


class ForgeHostTransportService(Protocol):
    @property
    def identity(self) -> Identity: ...

    def handle_request(self, envelope: RequestEnvelope) -> HostServiceOutcome: ...


def _protocol_error_code(error: Exception) -> str:
    if isinstance(error, _CapabilityUnavailableError):
        return "capability_unavailable"
    if isinstance(error, UnsupportedVersionError):
        return "unsupported_version"
    if isinstance(error, StaleIdentityError):
        return "stale_identity"
    if isinstance(error, StaleRevisionError):
        return "stale_revision"
    if isinstance(error, IdempotencyConflictError):
        return "idempotency_conflict"
    if isinstance(error, ProtocolValidationError | UnsafeSerializationError):
        return "invalid_message"
    if isinstance(
        error,
        ApprovalStateError | PlanStateError | ReplayUnavailableError | ForgeHostProtocolError,
    ):
        return "invalid_transition"
    return "internal_error"


class ForgeHostStdioServer:
    """Serve one authenticated generation over newline-delimited JSON."""

    def __init__(
        self,
        *,
        service: ForgeHostTransportService,
        input_stream: BinaryIO,
        output_stream: BinaryIO,
        server: Peer,
        capabilities: tuple[Capability, ...] = DEFAULT_HOST_CAPABILITIES,
        clock: Callable[[], datetime] | None = None,
        max_line_bytes: int = MAX_WIRE_LINE_BYTES,
    ) -> None:
        if server.host_kind != "engine":
            raise ValueError("Forge Host stdio server peer must use hostKind=engine")
        if max_line_bytes < 1024 or max_line_bytes > MAX_WIRE_LINE_BYTES:
            raise ValueError("Forge Host stdio line limit is outside the supported bound")
        if len({item.name for item in capabilities}) != len(capabilities):
            raise ValueError("Forge Host stdio capabilities must be unique")
        self._service = service
        self._input = input_stream
        self._output = output_stream
        self._server = server
        self._capabilities = tuple(capabilities)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_line_bytes = max_line_bytes
        self._handshake_complete = False
        self._negotiated_capabilities: frozenset[str] = frozenset()
        self._counter = 0
        self._write_lock = threading.RLock()

    @property
    def handshake_complete(self) -> bool:
        return self._handshake_complete

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Forge Host stdio clock must return an aware datetime")
        return value

    def _message_id(self, request: RequestEnvelope) -> str:
        self._counter += 1
        authority = (
            f"{self._service.identity.session_id}:{self._service.identity.generation_id}:"
            f"{request.request_id}:{self._counter}"
        ).encode()
        return "msg_" + hashlib.sha256(authority).hexdigest()[:24]

    def _write(self, *values: bytes) -> None:
        with self._write_lock:
            try:
                frames: list[bytes] = []
                for value in values:
                    if b"\n" in value or b"\r" in value:
                        raise ForgeHostStdioFatalError(
                            "Forge Host codec produced a non-JSONL-safe envelope"
                        )
                    framed = value + b"\n"
                    if len(framed) > self._max_line_bytes:
                        raise ForgeHostStdioFatalError(
                            "Forge Host output exceeded the wire byte limit"
                        )
                    frames.append(framed)
                for framed in frames:
                    remaining = memoryview(framed)
                    while remaining:
                        written = self._output.write(remaining)
                        if (
                            not isinstance(written, int)
                            or isinstance(written, bool)
                            or written <= 0
                            or written > len(remaining)
                        ):
                            raise ForgeHostStdioFatalError(
                                "Forge Host output did not complete its wire frame"
                            )
                        remaining = remaining[written:]
                self._output.flush()
            except ForgeHostStdioFatalError:
                raise
            except Exception:
                raise ForgeHostStdioFatalError(
                    "Forge Host output failed before the wire batch completed"
                ) from None

    def _response(
        self,
        request: RequestEnvelope,
        payload: object,
    ) -> ResponseEnvelope:
        response = ResponseEnvelope(
            protocol=PROTOCOL_NAME,
            protocol_version=PROTOCOL_VERSION,
            message_id=self._message_id(request),
            operation_id=request.operation_id,
            sent_at=self._now(),
            kind="response",
            reply_to=request.request_id,
            payload=payload,
        )
        validated = validate_envelope(encode_envelope(response))
        if not isinstance(validated, ResponseEnvelope):
            raise ForgeHostStdioFatalError("Forge Host response codec changed envelope kind")
        return validated

    def _error_response(self, request: RequestEnvelope, error: Exception) -> bytes:
        code = _protocol_error_code(error)
        payload = ProtocolError(
            type="protocol.error",
            code=code,
            message=_ERROR_SUMMARIES[code],
            retryable=code == "internal_error",
            current_identity=self._service.identity,
        )
        return encode_envelope(self._response(request, payload))

    def _require_capabilities(self, request: RequestEnvelope) -> None:
        payload = request.payload
        if isinstance(payload, PlannerInput):
            required = frozenset({"forge.plan"})
        elif isinstance(payload, ApprovalDecision):
            required = frozenset({"forge.approval"})
        elif isinstance(payload, RunStart):
            required = frozenset({"forge.execute", "forge.evidence"})
        elif isinstance(payload, RunCancel):
            raise _CapabilityUnavailableError(
                "run cancellation is not available in the Phase 0 transport"
            )
        else:
            required = frozenset()
        if not required.issubset(self._negotiated_capabilities):
            raise _CapabilityUnavailableError(
                "the command requires capabilities that were not negotiated"
            )

    def _validated_outcome(
        self,
        request: RequestEnvelope,
        outcome: HostServiceOutcome,
    ) -> tuple[bytes, ...]:
        try:
            response = validate_envelope(outcome.response_bytes)
        except Exception:
            raise ForgeHostStdioFatalError(
                "Forge Host service returned an invalid response envelope"
            ) from None
        response_identity = getattr(response.payload, "identity", None)
        current_identity = getattr(response.payload, "current_identity", None)
        identity = (
            response_identity
            if isinstance(response_identity, Identity)
            else current_identity
            if isinstance(current_identity, Identity)
            else None
        )
        if (
            not isinstance(response, ResponseEnvelope)
            or response != outcome.response
            or response.reply_to != request.request_id
            or response.operation_id != request.operation_id
            or encode_envelope(response) != outcome.response_bytes
            or identity is None
            or identity.session_id != self._service.identity.session_id
            or identity.generation_id != self._service.identity.generation_id
        ):
            raise ForgeHostStdioFatalError(
                "Forge Host service response authority did not match the request"
            )
        if len(outcome.events) != len(outcome.event_bytes):
            raise ForgeHostStdioFatalError(
                "Forge Host service event objects and bytes did not match"
            )
        for event_model, event_bytes in zip(outcome.events, outcome.event_bytes, strict=True):
            try:
                event = validate_envelope(event_bytes)
            except Exception:
                raise ForgeHostStdioFatalError(
                    "Forge Host service returned an invalid event envelope"
                ) from None
            if (
                not isinstance(event, EventEnvelope)
                or event != event_model
                or not self._event_operation_matches_request(event, request)
                or encode_envelope(event) != event_bytes
                or event.identity.session_id != self._service.identity.session_id
                or event.identity.generation_id != self._service.identity.generation_id
            ):
                raise ForgeHostStdioFatalError(
                    "Forge Host service event authority did not match the request"
                )
        return (outcome.response_bytes, *outcome.event_bytes)

    @staticmethod
    def _event_operation_matches_request(
        event: EventEnvelope,
        request: RequestEnvelope,
    ) -> bool:
        # approval.required mints a fresh, fingerprint-bound operation authority that
        # the later approval.decision echoes separately from its own command operation.
        # All other Phase-0 events remain correlated to the request that caused them.
        if isinstance(event.payload, ApprovalRequired):
            return event.operation_id != request.operation_id
        return event.operation_id == request.operation_id

    def _handle(self, request: RequestEnvelope) -> tuple[bytes, ...]:
        if isinstance(request.payload, HandshakeRequest):
            if self._handshake_complete:
                raise PlanStateError("handshake is already complete")
            payload = negotiate_handshake(
                request.payload,
                server=self._server,
                server_capabilities=self._capabilities,
                identity=self._service.identity,
            )
            response = self._response(request, payload)
            self._negotiated_capabilities = frozenset(
                capability.name for capability in payload.capabilities
            )
            return (encode_envelope(response),)
        if not self._handshake_complete:
            raise PlanStateError("handshake is required before generation commands")
        self._require_capabilities(request)
        outcome = self._service.handle_request(request)
        return self._validated_outcome(request, outcome)

    def serve(self) -> int:
        """Run until clean EOF; return the number of correlated requests handled."""

        handled = 0
        while True:
            raw = self._input.readline(self._max_line_bytes + 1)
            if raw == b"":
                return handled
            if len(raw) > self._max_line_bytes:
                raise ForgeHostStdioFatalError("Forge Host input exceeded the wire byte limit")
            if not raw.endswith(b"\n"):
                raise ForgeHostStdioFatalError("Forge Host input ended without a JSONL delimiter")
            wire = raw.rstrip(b"\r\n")
            if not wire:
                raise ForgeHostStdioFatalError("Forge Host input contained an empty frame")
            try:
                envelope = validate_envelope(wire)
            except Exception:
                raise ForgeHostStdioFatalError(
                    "Forge Host input could not be correlated safely"
                ) from None
            if not isinstance(envelope, RequestEnvelope):
                raise ForgeHostStdioFatalError("Forge Host input was not a request envelope")
            try:
                values = self._handle(envelope)
            except ForgeHostStdioFatalError:
                raise
            except Exception as exc:  # noqa: BLE001 - stable non-reflective wire error
                if envelope.operation_id is None:
                    raise ForgeHostStdioFatalError(
                        "Forge Host handshake failed before safe error correlation"
                    ) from None
                try:
                    values = (self._error_response(envelope, exc),)
                except Exception:
                    raise ForgeHostStdioFatalError(
                        "Forge Host could not emit a correlated protocol error"
                    ) from None
            self._write(*values)
            if isinstance(envelope.payload, HandshakeRequest):
                self._handshake_complete = True
            handled += 1


__all__ = [
    "DEFAULT_HOST_CAPABILITIES",
    "ForgeHostStdioFatalError",
    "ForgeHostStdioServer",
    "ForgeHostTransportService",
    "MAX_WIRE_LINE_BYTES",
]
