from __future__ import annotations

import io
import json
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from alysis_code.forge_host_protocol.codec import encode_envelope, validate_envelope
from alysis_code.forge_host_protocol.models import (
    ApprovalRequired,
    Capability,
    CommandAccepted,
    EventEnvelope,
    HandshakeResponse,
    Identity,
    Peer,
    ProtocolError,
    RequestEnvelope,
    ResponseEnvelope,
)
from alysis_code.forge_host_service import HostServiceOutcome
from alysis_code.forge_host_stdio import (
    ForgeHostStdioFatalError,
    ForgeHostStdioServer,
)

NOW = datetime(2026, 8, 21, 9, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parents[1] / "tests" / "fixtures" / "forge_host_v2" / "valid"


def _fixture_mapping(name: str) -> dict[str, Any]:
    value = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _fixture_envelope(name: str) -> RequestEnvelope | EventEnvelope:
    envelope = validate_envelope(_fixture_mapping(name))
    assert isinstance(envelope, RequestEnvelope | EventEnvelope)
    return envelope


def _wire(envelope: RequestEnvelope | EventEnvelope) -> bytes:
    return encode_envelope(envelope) + b"\n"


class _RecordingOutput(io.BytesIO):
    def __init__(self) -> None:
        super().__init__()
        self.flush_calls = 0

    def flush(self) -> None:
        self.flush_calls += 1
        super().flush()


class _ChunkedOutput(_RecordingOutput):
    def write(self, value: Any) -> int:
        return super().write(value[:7])


class _ZeroOutput(_RecordingOutput):
    def write(self, _value: Any) -> int:
        return 0


class _Service:
    def __init__(
        self,
        *,
        fail: Exception | None = None,
        corrupt: bool = False,
        foreign_response: bool = False,
        foreign_event: bool = False,
        include_approval: bool = False,
    ) -> None:
        self._identity = Identity(
            session_id="ses_abcdefgh",
            generation_id="gen_abcdefgh",
            run_id=None,
            revision=1,
        )
        self.fail = fail
        self.corrupt = corrupt
        self.foreign_response = foreign_response
        self.foreign_event = foreign_event
        self.include_approval = include_approval
        self.calls: list[RequestEnvelope] = []
        self.last_outcome: HostServiceOutcome | None = None

    @property
    def identity(self) -> Identity:
        return self._identity

    def handle_request(self, envelope: RequestEnvelope) -> HostServiceOutcome:
        self.calls.append(envelope)
        if self.fail is not None:
            raise self.fail
        response_identity = (
            Identity(
                session_id="ses_foreign01",
                generation_id="gen_abcdefgh",
                run_id=None,
                revision=1,
            )
            if self.foreign_response
            else self.identity
        )
        response = ResponseEnvelope(
            protocol="alysis.forge-host",
            protocol_version="2.0",
            message_id="msg_service0001",
            operation_id=envelope.operation_id,
            sent_at=NOW,
            kind="response",
            reply_to=envelope.request_id,
            payload=CommandAccepted(
                type="command.accepted",
                identity=response_identity,
                duplicate=False,
            ),
        )
        event = _fixture_envelope("plan-ready-event.json")
        assert isinstance(event, EventEnvelope)
        if self.foreign_event:
            event_wire = event.model_dump(mode="json", by_alias=True, exclude_none=False)
            event_wire["identity"]["sessionId"] = "ses_foreign01"
            foreign = validate_envelope(event_wire)
            assert isinstance(foreign, EventEnvelope)
            event = foreign
        events = [event]
        if self.include_approval:
            approval = _fixture_envelope("approval-required-event.json")
            assert isinstance(approval, EventEnvelope)
            events.append(approval)
        response_bytes = encode_envelope(response)
        event_bytes = tuple(encode_envelope(item) for item in events)
        if self.corrupt:
            event_bytes = (b'{"kind":"event","credential":"sk-private-value"}',)
        outcome = HostServiceOutcome(
            response=response,
            events=tuple(events),
            response_bytes=response_bytes,
            event_bytes=event_bytes,
            replayed=False,
        )
        self.last_outcome = outcome
        return outcome


def _server(
    input_bytes: bytes,
    *,
    service: _Service | None = None,
    capabilities: tuple[Capability, ...] | None = None,
    max_line_bytes: int = 8192,
    output: _RecordingOutput | None = None,
) -> tuple[ForgeHostStdioServer, _Service, _RecordingOutput]:
    actual_service = service or _Service()
    actual_output = output or _RecordingOutput()
    kwargs: dict[str, Any] = {}
    if capabilities is not None:
        kwargs["capabilities"] = capabilities
    server = ForgeHostStdioServer(
        service=actual_service,
        input_stream=io.BytesIO(input_bytes),
        output_stream=actual_output,
        server=Peer(name="AlysisEngine", version="2.0.0", host_kind="engine"),
        clock=lambda: NOW,
        max_line_bytes=max_line_bytes,
        **kwargs,
    )
    return server, actual_service, actual_output


def test_stdio_negotiates_then_streams_exact_service_response_and_event_bytes() -> None:
    handshake_wire = _fixture_mapping("handshake-request.json")
    handshake_wire["payload"]["capabilities"].append({"name": "forge.replay", "version": 1})
    handshake = validate_envelope(handshake_wire)
    planner = _fixture_envelope("planner-input-request.json")
    assert isinstance(handshake, RequestEnvelope)
    assert isinstance(planner, RequestEnvelope)
    server, service, output = _server(_wire(handshake) + _wire(planner))

    assert server.serve() == 2
    assert server.handshake_complete
    assert service.calls == [planner]
    assert service.last_outcome is not None
    lines = output.getvalue().splitlines()
    assert len(lines) == 3
    response = validate_envelope(lines[0])
    assert isinstance(response, ResponseEnvelope)
    assert isinstance(response.payload, HandshakeResponse)
    assert response.payload.selected_version == "2.0"
    assert [(item.name, item.version) for item in response.payload.capabilities] == [
        ("forge.plan", 1)
    ]
    assert all(item.name != "forge.replay" for item in response.payload.capabilities)
    assert lines[1] == service.last_outcome.response_bytes
    assert lines[2] == service.last_outcome.event_bytes[0]
    assert output.flush_calls == 2


def test_stdio_forwards_fresh_fingerprint_bound_approval_operation_authority() -> None:
    handshake = _fixture_envelope("handshake-request.json")
    planner = _fixture_envelope("planner-input-request.json")
    assert isinstance(handshake, RequestEnvelope)
    assert isinstance(planner, RequestEnvelope)
    service = _Service(include_approval=True)
    server, _service, output = _server(
        _wire(handshake) + _wire(planner),
        service=service,
    )

    assert server.serve() == 2
    assert service.last_outcome is not None
    lines = output.getvalue().splitlines()
    assert len(lines) == 4
    ready = validate_envelope(lines[2])
    approval = validate_envelope(lines[3])
    assert isinstance(ready, EventEnvelope)
    assert isinstance(approval, EventEnvelope)
    assert isinstance(approval.payload, ApprovalRequired)
    assert ready.operation_id == planner.operation_id
    assert approval.operation_id != planner.operation_id
    assert lines[2:] == list(service.last_outcome.event_bytes)


def test_command_before_handshake_returns_stable_correlated_transition_error() -> None:
    planner = _fixture_envelope("planner-input-request.json")
    assert isinstance(planner, RequestEnvelope)
    server, service, output = _server(_wire(planner))

    assert server.serve() == 1
    assert service.calls == []
    response = validate_envelope(output.getvalue().strip())
    assert isinstance(response, ResponseEnvelope)
    assert isinstance(response.payload, ProtocolError)
    assert response.payload.code == "invalid_transition"
    assert response.payload.message == "The protocol transition is invalid."
    assert not response.payload.retryable


def test_unnegotiated_command_capability_is_rejected_before_service() -> None:
    handshake_wire = _fixture_mapping("handshake-request.json")
    handshake_wire["payload"]["capabilities"] = [{"name": "forge.execute", "version": 1}]
    handshake = validate_envelope(handshake_wire)
    planner = _fixture_envelope("planner-input-request.json")
    assert isinstance(handshake, RequestEnvelope)
    assert isinstance(planner, RequestEnvelope)
    server, service, output = _server(_wire(handshake) + _wire(planner))

    assert server.serve() == 2
    assert service.calls == []
    response = validate_envelope(output.getvalue().splitlines()[-1])
    assert isinstance(response, ResponseEnvelope)
    assert isinstance(response.payload, ProtocolError)
    assert response.payload.code == "capability_unavailable"
    assert response.payload.message == "The requested capability is unavailable."


def test_phase0_run_cancel_is_capability_unavailable_before_service() -> None:
    handshake_wire = _fixture_mapping("handshake-request.json")
    handshake_wire["payload"]["capabilities"] = [{"name": "forge.execute", "version": 1}]
    handshake = validate_envelope(handshake_wire)
    cancel = _fixture_envelope("run-cancel-request.json")
    assert isinstance(handshake, RequestEnvelope)
    assert isinstance(cancel, RequestEnvelope)
    server, service, output = _server(_wire(handshake) + _wire(cancel))

    assert server.serve() == 2
    assert service.calls == []
    response = validate_envelope(output.getvalue().splitlines()[-1])
    assert isinstance(response, ResponseEnvelope)
    assert isinstance(response.payload, ProtocolError)
    assert response.payload.code == "capability_unavailable"


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError, TypeError])
def test_service_failure_is_redacted_into_stable_internal_error(
    error_type: type[Exception],
) -> None:
    handshake = _fixture_envelope("handshake-request.json")
    planner = _fixture_envelope("planner-input-request.json")
    assert isinstance(handshake, RequestEnvelope)
    assert isinstance(planner, RequestEnvelope)
    secret = "sk-private-value at /Users/private/workspace"
    server, service, output = _server(
        _wire(handshake) + _wire(planner),
        service=_Service(fail=error_type(secret)),
    )

    assert server.serve() == 2
    assert len(service.calls) == 1
    final_wire = output.getvalue().splitlines()[-1]
    assert secret.encode() not in final_wire
    assert b"/Users/private" not in final_wire
    response = validate_envelope(final_wire)
    assert isinstance(response, ResponseEnvelope)
    assert isinstance(response.payload, ProtocolError)
    assert response.payload.code == "internal_error"
    assert response.payload.message == "The protocol operation failed."
    assert response.payload.retryable


def test_service_error_and_error_clock_double_fault_remain_non_reflective() -> None:
    handshake = _fixture_envelope("handshake-request.json")
    planner = _fixture_envelope("planner-input-request.json")
    assert isinstance(handshake, RequestEnvelope)
    assert isinstance(planner, RequestEnvelope)
    secret = "sk-private-value at /Users/private/workspace"
    service = _Service(fail=RuntimeError(secret))
    output = _RecordingOutput()
    clock_calls = 0

    def failing_clock() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        return NOW if clock_calls == 1 else datetime(2026, 8, 21, 9, 0)

    server = ForgeHostStdioServer(
        service=service,
        input_stream=io.BytesIO(_wire(handshake) + _wire(planner)),
        output_stream=output,
        server=Peer(name="AlysisEngine", version="2.0.0", host_kind="engine"),
        clock=failing_clock,
        max_line_bytes=8192,
    )

    with pytest.raises(ForgeHostStdioFatalError, match="correlated protocol error") as caught:
        server.serve()
    rendered = "".join(traceback.format_exception(caught.value))
    assert secret not in rendered
    assert "/Users/private/workspace" not in rendered
    assert len(output.getvalue().splitlines()) == 1


def test_corrupt_service_event_is_fatal_and_is_never_forwarded() -> None:
    handshake = _fixture_envelope("handshake-request.json")
    planner = _fixture_envelope("planner-input-request.json")
    assert isinstance(handshake, RequestEnvelope)
    assert isinstance(planner, RequestEnvelope)
    server, _service, output = _server(
        _wire(handshake) + _wire(planner),
        service=_Service(corrupt=True),
    )

    with pytest.raises(ForgeHostStdioFatalError, match="invalid event envelope"):
        server.serve()
    lines = output.getvalue().splitlines()
    assert len(lines) == 1
    assert b"sk-private-value" not in output.getvalue()


@pytest.mark.parametrize("foreign_field", ["response", "event"])
def test_foreign_generation_authority_is_never_forwarded(foreign_field: str) -> None:
    handshake = _fixture_envelope("handshake-request.json")
    planner = _fixture_envelope("planner-input-request.json")
    assert isinstance(handshake, RequestEnvelope)
    assert isinstance(planner, RequestEnvelope)
    service = _Service(
        foreign_response=foreign_field == "response",
        foreign_event=foreign_field == "event",
    )
    server, _service, output = _server(
        _wire(handshake) + _wire(planner),
        service=service,
    )

    with pytest.raises(ForgeHostStdioFatalError, match="authority"):
        server.serve()
    lines = output.getvalue().splitlines()
    assert len(lines) == 1
    assert b"ses_foreign01" not in output.getvalue()


@pytest.mark.parametrize(
    ("input_bytes", "max_line_bytes", "message"),
    [
        (b'{"credential":"sk-private-value"}\n', 4096, "correlated safely"),
        (b"x" * 1025 + b"\n", 1024, "wire byte limit"),
        (b'{"unterminated":true}', 4096, "JSONL delimiter"),
        (b"\n", 4096, "empty frame"),
    ],
)
def test_uncorrelatable_frames_fail_closed_without_reflection(
    input_bytes: bytes,
    max_line_bytes: int,
    message: str,
) -> None:
    server, service, output = _server(input_bytes, max_line_bytes=max_line_bytes)

    with pytest.raises(ForgeHostStdioFatalError, match=message) as caught:
        server.serve()
    assert service.calls == []
    assert output.getvalue() == b""
    assert "sk-private-value" not in str(caught.value)
    rendered = "".join(traceback.format_exception(caught.value))
    assert "sk-private-value" not in rendered


def test_validation_traceback_cannot_reflect_private_planner_input() -> None:
    invalid = _fixture_mapping("planner-input-request.json")
    invalid["payload"]["expectedRevision"] = 0
    invalid["payload"]["input"]["text"] = (
        "The invalid input mentions /Users/private/workspace and sk-private-value."
    )
    raw = json.dumps(invalid).encode() + b"\n"
    server, service, output = _server(raw)

    with pytest.raises(ForgeHostStdioFatalError, match="correlated safely") as caught:
        server.serve()
    rendered = "".join(traceback.format_exception(caught.value))
    assert service.calls == []
    assert output.getvalue() == b""
    assert "/Users/private/workspace" not in rendered
    assert "sk-private-value" not in rendered


def test_handshake_negotiation_failure_is_fatal_because_operation_is_uncorrelated() -> None:
    handshake_wire = _fixture_mapping("handshake-request.json")
    handshake_wire["payload"]["supportedVersions"] = ["1.0"]
    handshake = validate_envelope(handshake_wire)
    assert isinstance(handshake, RequestEnvelope)
    server, service, output = _server(_wire(handshake))

    with pytest.raises(ForgeHostStdioFatalError, match="safe error correlation"):
        server.serve()
    assert service.calls == []
    assert output.getvalue() == b""


def test_second_handshake_closes_the_connection_without_emitting_invalid_response() -> None:
    handshake = _fixture_envelope("handshake-request.json")
    assert isinstance(handshake, RequestEnvelope)
    server, service, output = _server(_wire(handshake) + _wire(handshake))

    with pytest.raises(ForgeHostStdioFatalError, match="safe error correlation"):
        server.serve()
    assert service.calls == []
    lines = output.getvalue().splitlines()
    assert len(lines) == 1
    first = validate_envelope(lines[0])
    assert isinstance(first, ResponseEnvelope)
    assert isinstance(first.payload, HandshakeResponse)


def test_output_writer_completes_short_writes_before_committing_handshake() -> None:
    handshake = _fixture_envelope("handshake-request.json")
    assert isinstance(handshake, RequestEnvelope)
    chunked = _ChunkedOutput()
    server, _service, output = _server(_wire(handshake), output=chunked)

    assert server.serve() == 1
    assert server.handshake_complete
    response = validate_envelope(output.getvalue().strip())
    assert isinstance(response, ResponseEnvelope)
    assert isinstance(response.payload, HandshakeResponse)


def test_zero_length_output_write_is_fatal_before_handshake_commit() -> None:
    handshake = _fixture_envelope("handshake-request.json")
    assert isinstance(handshake, RequestEnvelope)
    zero = _ZeroOutput()
    server, _service, _output = _server(_wire(handshake), output=zero)

    with pytest.raises(ForgeHostStdioFatalError, match="did not complete"):
        server.serve()
    assert not server.handshake_complete


def test_configured_wire_limit_applies_to_output_frames() -> None:
    handshake = _fixture_envelope("handshake-request.json")
    planner = _fixture_envelope("planner-input-request.json")
    assert isinstance(handshake, RequestEnvelope)
    assert isinstance(planner, RequestEnvelope)
    server, service, output = _server(
        _wire(handshake) + _wire(planner),
        max_line_bytes=4096,
    )

    with pytest.raises(ForgeHostStdioFatalError, match="output exceeded"):
        server.serve()
    assert len(service.calls) == 1
    assert len(output.getvalue().splitlines()) == 1
    assert server.handshake_complete
