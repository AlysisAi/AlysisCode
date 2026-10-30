"""Actual raw bridge decoder and bounded asynchronous admission exchange."""

import copy
import json
import threading
from types import SimpleNamespace

import pytest
from test_computer_start_resolved_contract import CORPUS, entry
from test_ide_computer_use_host import _bridge_session, _wait_for_event

from alysis_code.agent.browser_start import BrowserStartHandoff
from alysis_code.ide.protocol import ProtocolError, parse_request_line
from alysis_code.ide.stdio_bridge import BridgeCancellationError


@pytest.fixture(autouse=True)
def mac_w7_bridge_harness(monkeypatch):
    from alysis_code import computer_use_platform_policy as policy

    monkeypatch.setattr(policy, "sys", SimpleNamespace(platform="darwin"))


def setup(tmp_path):
    bridge, out, session, job = _bridge_session(tmp_path)
    e, _, cfg = entry()
    e.mode = "resolved"
    session.sensitive_media = False
    session.computer_use_host = None
    session.computer_start_resolved_host = copy.deepcopy(CORPUS["bases"]["descriptor"])
    session.agent_session = SimpleNamespace(
        resolved_entry=e, client=e.client, stream=False, cfg=cfg, close=lambda: None
    )
    results = []

    def invoke():
        session.surface.with_job(job.job_id)
        session.surface.emit_tool_call_started("call-1", "computer_start_resolved_v1", "{}")
        try:
            results.append(
                bridge._request_resolved_start(
                    session, CORPUS["bases"]["plan"], CORPUS["bases"]["selection"], "call-1"
                )
            )
        except BaseException as error:
            results.append(error)

    worker = threading.Thread(target=invoke)
    worker.start()
    event = _wait_for_event(out, "computer_start_resolved_request")
    identities = {k: v for k, v in event["payload"].items() if k not in {"plan", "selection"}}
    identities.update(session_id=event["session_id"], job_id=event["job_id"])
    return bridge, out, session, job, worker, results, identities


def respond(bridge, params, raw_mutation=lambda s: s):
    raw = json.dumps(
        {
            "protocol_version": "1",
            "id": "reply",
            "method": "computer_start_resolved.respond",
            "params": params,
        }
    )
    return bridge._resolved_start_respond(parse_request_line(raw_mutation(raw)))


def finish(bridge, job, worker):
    job.cancellation_event.set()
    worker.join(1)
    job.status = "completed"
    bridge.close()


def test_raw_success_exact_envelope_no_event_retention(tmp_path):
    bridge, out, s, j, w, result, ids = setup(tmp_path)
    try:
        ack = respond(bridge, {**ids, "allow": True})
        assert ack == {"status": "accepted", "request_id": ids["request_id"]}
        w.join(1)
        assert result == [BrowserStartHandoff(ids["request_id"])]
        assert s.computer_start_resolved_host is None
        assert "computer_start_resolved_request" not in str(bridge._event_buffers)
        with pytest.raises(ProtocolError):
            respond(bridge, {**ids, "allow": True})
    finally:
        finish(bridge, j, w)


@pytest.mark.parametrize(
    "kind", ["extra", "duplicate", "integer_token", "unknown_code", "w3_missing", "w3_foreign"]
)
def test_owned_malformed_response_poisoned_without_waiting(tmp_path, kind):
    bridge, out, s, j, w, result, ids = setup(tmp_path)
    try:
        value = {**ids, "allow": True}

        def mutation(raw):
            if kind == "duplicate":
                return raw.replace('"allow": true', '"allow": false,"allow": true')
            if kind == "integer_token":
                return raw.replace('"version": 1', '"version": 1.0')
            return raw

        if kind == "extra":
            value["extra"] = "untrusted"
        if kind == "unknown_code":
            value.update(allow=False, code="unexpected")
        if kind.startswith("w3_"):
            value.update(allow=False, code="computer_use_target_unverified")
            if kind == "w3_foreign":
                value["unverified_target_ids"] = ["10000000-0000-4000-8000-000000000099"]
        with pytest.raises(ProtocolError):
            respond(bridge, value, mutation)
        w.join(1)
        assert result == [{"error": "computer_start_response_invalid"}]
        assert not s.browser_start_consumed
    finally:
        finish(bridge, j, w)


def test_foreign_reply_does_not_poison_current_pending(tmp_path):
    bridge, out, s, j, w, result, ids = setup(tmp_path)
    try:
        with pytest.raises(ProtocolError):
            respond(bridge, {**ids, "request_id": "foreign", "allow": True})
        assert not s.pending_resolved_start["done"].is_set()
        respond(bridge, {**ids, "allow": True})
        w.join(1)
        assert type(result[0]) is BrowserStartHandoff
    finally:
        finish(bridge, j, w)


def test_stop_wins_before_allow_and_late_reply(tmp_path):
    bridge, out, s, j, w, result, ids = setup(tmp_path)
    try:
        j.cancellation_event.set()
        with pytest.raises(ProtocolError):
            respond(bridge, {**ids, "allow": True})
        w.join(1)
        assert isinstance(result[0], BridgeCancellationError)
        assert not s.browser_start_consumed and s.pending_resolved_start is None
    finally:
        finish(bridge, j, w)


def test_scoped_create_decode_preserves_T_isolation():
    d = CORPUS["bases"]["descriptor"]
    raw = json.dumps(
        {
            "protocol_version": "1",
            "id": "create",
            "method": "session.create",
            "params": {"computer_start_resolved_host": d},
            "computer_use_attribution": {"bad": 1.5},
        }
    )
    assert parse_request_line(raw).params["computer_start_resolved_host"] == d
    with pytest.raises(ProtocolError):
        parse_request_line(raw.replace('"version": 1', '"version": 1e0', 1))
    with pytest.raises(ProtocolError):
        parse_request_line(raw.replace('"host_nonce":', '"host_nonce":"bad","host_nonce":', 1))


def test_correlated_protocol_unavailable_is_only_initialization_error(tmp_path, monkeypatch):
    import io

    from alysis_code.agent.computer_start_resolved import ProtocolUnavailable
    from alysis_code.config import AppConfig
    from alysis_code.ide.stdio_bridge import StdioBridge

    monkeypatch.setattr(
        "alysis_code.ide.stdio_bridge.load_config", lambda: AppConfig(model="fixture")
    )

    def unsupported(**_):
        raise ProtocolUnavailable()

    output = io.StringIO()
    bridge = StdioBridge(stdout=output, create_session_fn=unsupported)
    bridge.process_line(
        json.dumps(
            {
                "protocol_version": "1",
                "id": "create-1",
                "method": "session.create",
                "params": {
                    "session_id": "unqualified",
                    "workspace": str(tmp_path),
                    "mode": "review",
                    "computer_start_resolved_host": CORPUS["bases"]["descriptor"],
                },
            }
        )
    )
    reply = json.loads(output.getvalue().splitlines()[-1])
    assert reply == CORPUS["bases"]["protocolUnavailable"]
    assert "unqualified" not in bridge._sessions
    bridge.close()


def test_private_echo_preserves_secret_shaped_label_on_wire(tmp_path, monkeypatch):
    import io

    from alysis_code.config import AppConfig
    from alysis_code.ide.stdio_bridge import StdioBridge

    monkeypatch.setattr(
        "alysis_code.ide.stdio_bridge.load_config", lambda: AppConfig(model="fixture")
    )
    descriptor = copy.deepcopy(CORPUS["bases"]["descriptor"])
    descriptor["discovery"]["targets"][0]["name"] = "sk-SYNTHETIC_private_label_12345"
    output = io.StringIO()
    bridge = StdioBridge(
        stdout=output, create_session_fn=lambda **_: SimpleNamespace(close=lambda: None)
    )
    bridge.process_line(
        json.dumps(
            {
                "protocol_version": "1",
                "id": "create-1",
                "method": "session.create",
                "params": {
                    "session_id": "qualified",
                    "workspace": str(tmp_path),
                    "mode": "review",
                    "computer_start_resolved_host": descriptor,
                },
            }
        )
    )
    reply = json.loads(output.getvalue().splitlines()[-1])
    assert reply["result"]["computer_start_resolved_host"] == descriptor
    assert descriptor["discovery"]["targets"][0]["name"] not in str(bridge._event_buffers)
    bridge.close()


@pytest.mark.parametrize("session_id", [123, True, None, " padded ", "x" * 257])
def test_v_session_identity_not_coerced_or_trimmed(tmp_path, monkeypatch, session_id):
    from alysis_code.ide.protocol import ProtocolRequest
    from alysis_code.ide.stdio_bridge import StdioBridge

    bridge = StdioBridge(create_session_fn=lambda **_: pytest.fail("invalid identity initialized"))
    with pytest.raises(ProtocolError, match="identity"):
        bridge._session_create(
            ProtocolRequest(
                id="create",
                method="session.create",
                params={
                    "session_id": session_id,
                    "workspace": str(tmp_path),
                    "mode": "review",
                    "computer_start_resolved_host": CORPUS["bases"]["descriptor"],
                },
            )
        )
    assert not bridge._sessions
    bridge.close()


def test_V_lexical_boundary_precedes_generic_bridge_decode(monkeypatch):
    import alysis_code.ide.protocol as protocol
    from alysis_code.llm import resolved_entry

    original = resolved_entry.validate_bridge_request
    calls = []

    def lexical(raw, payload=None):
        calls.append("lexical")
        return original(raw, payload)

    def generic(raw):
        calls.append("generic")
        return json.loads(raw)

    monkeypatch.setattr(resolved_entry, "validate_bridge_request", lexical)
    monkeypatch.setattr(protocol, "json", SimpleNamespace(loads=generic))
    protocol.parse_request_line(
        json.dumps(
            {
                "protocol_version": "1",
                "id": "r",
                "method": "session.create",
                "params": {"computer_start_resolved_host": CORPUS["bases"]["descriptor"]},
            }
        )
    )
    assert calls[0] == "lexical"


def test_oversized_v_descriptor_rejected_before_generic_decode(monkeypatch):
    import alysis_code.ide.protocol as protocol

    raw = json.dumps(
        {
            "protocol_version": "1",
            "id": "bounded",
            "method": "session.create",
            "params": {"computer_start_resolved_host": {"padding": "x" * 131073}},
        }
    )
    monkeypatch.setattr(
        protocol,
        "json",
        SimpleNamespace(
            loads=lambda _: pytest.fail("oversized descriptor reached generic decoder")
        ),
    )
    with pytest.raises(ProtocolError) as caught:
        protocol.parse_request_line(raw)
    assert caught.value.code == "invalid_params"
    assert caught.value.request_id == "bounded"


@pytest.mark.parametrize("loss", ["capability", "owner"])
@pytest.mark.parametrize("phase", ["pending", "after_ack"])
def test_in_flight_loss_has_exact_closed_outcome(tmp_path, loss, phase):
    from alysis_code.agent.computer_start_resolved import Refusal

    bridge, out, session, job, worker, results, ids = setup(tmp_path)
    try:
        with bridge._state_lock:
            if phase == "after_ack":
                respond(bridge, {**ids, "allow": True})
            if loss == "capability":
                session.agent_session.cfg = SimpleNamespace(model="changed")
            else:
                session.active_job = None
        worker.join(1)
        code = (
            "computer_start_capability_lost"
            if loss == "capability"
            else "computer_start_owner_invalid"
        )
        assert results == [Refusal(code, "unknown")]
        assert results[0]["fresh_turn_required"] is True
        assert not session.browser_start_consumed
        assert session.pending_resolved_start is None
    finally:
        finish(bridge, job, worker)
