from __future__ import annotations

import json
import threading
from dataclasses import replace

import pytest
from test_computer_use_approval_expiry import TOKEN, fixture
from test_computer_use_approval_protocol import _respond_params
from test_computer_use_uploads import setup, upload
from test_ide_computer_use_host import _bridge_session, _lines, _request, _wait_for_event

from alysis_code.agent.computer_use_tools import build_computer_use_tools
from alysis_code.ide.stdio_bridge import BridgeCancellationError

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")


@pytest.mark.parametrize("review_seconds", [18, 19])
@pytest.mark.parametrize("required", [True, False])
def test_upload_approval_uses_original_host_deadline_and_cannot_omit_review(
    tmp_path, monkeypatch, review_seconds, required
):
    bridge, session, _job, clock, _records, _waits, events, calls, handler, allow, _run = fixture(
        tmp_path, monkeypatch, transport=11, required=required
    )
    session.computer_use_host = replace(session.computer_use_host, uploads_available=True)

    def review(event):
        if event["type"] == "approval.required":
            clock.advance(review_seconds)
            allow()

    handler[0] = review
    result = bridge._request_computer_use(
        session,
        "computer_upload",
        {
            "element_id": "native-input",
            "snapshot_id": "original",
            "approval_token": "MODEL_TOKEN",
            "approval_phase": "execute",
        },
    )
    assert len(calls) == (2 if review_seconds == 18 else 1)
    assert any(event["type"] == "approval.required" for event in events)
    assert "approval_token" not in calls[0]
    if review_seconds == 18:
        assert calls[-1] == {
            "element_id": "native-input",
            "snapshot_id": "original",
            "approval_phase": "execute",
            "approval_token": TOKEN,
        }
    else:
        assert result["error"] == "computer_use_approval_expired"
    assert TOKEN not in json.dumps(events) and "MODEL_TOKEN" not in json.dumps(events)


def test_setup_returns_without_waiting_or_dispatch_and_cannot_return_disclosed_preflight(
    tmp_path, monkeypatch
):
    bridge, session, _job, _clock, records, waits, _events, _calls, _handler, _allow, _run = (
        fixture(tmp_path, monkeypatch)
    )
    session.computer_use_host = replace(session.computer_use_host, uploads_available=True)
    calls = []
    response = [setup()]
    monkeypatch.setattr(
        bridge,
        "_exchange_computer_use",
        lambda _session, _tool, args: calls.append(args.copy()) or response[0],
    )
    assert (
        bridge._request_computer_use(
            session, "computer_upload", {"element_id": "input", "snapshot_id": "original"}
        )
        == setup()
    )
    assert len(calls) == 1 and calls[0]["approval_phase"] == "prepare"
    assert not records and not waits
    response[0] = upload()
    assert (
        bridge._request_computer_use(
            session, "computer_upload", {"element_id": "input", "snapshot_id": "original"}
        )["error"]
        == "invalid_upload_response"
    )
    assert len(calls) == 2


def test_cancel_during_review_never_sends_upload_execute(tmp_path, monkeypatch):
    bridge, session, job, _clock, _records, _waits, _events, calls, handler, allow, _run = fixture(
        tmp_path, monkeypatch
    )
    session.computer_use_host = replace(session.computer_use_host, uploads_available=True)

    def review(event):
        if event["type"] == "approval.required":
            allow()
            job.cancellation_event.set()

    handler[0] = review
    with pytest.raises(BridgeCancellationError):
        bridge._request_computer_use(
            session, "computer_upload", {"element_id": "input", "snapshot_id": "original"}
        )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "field", ["session_id", "job_id", "operation_id", "tool_call_id", "tool_name", "response_nonce"]
)
def test_actual_pending_upload_status_refuses_wrong_binding_then_accepts_exact_response(
    tmp_path, field
):
    bridge, out, session, job = _bridge_session(tmp_path)
    session.computer_use_host = replace(session.computer_use_host, uploads_available=True)
    result, errors = {}, []

    def run():
        session.surface.with_job(job.job_id)
        session.surface.emit_tool_call_started(
            "upload-status-call", "computer_upload_status", "[redacted]"
        )
        try:
            tools = build_computer_use_tools(
                lambda name, args: bridge._request_computer_use(session, name, args),
                uploads_available=True,
            )
            result.update(tools["computer_upload_status"].run({"upload_id": "upload-1"}))
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        request = _wait_for_event(out, "computer_use_request")
        valid = _respond_params(session, request, upload())
        bridge.process_line(
            _request("computer.respond", {**valid, field: "wrong-binding"}, "wrong")
        )
        assert next(line for line in _lines(out) if line.get("id") == "wrong")["ok"] is False
        assert not result and worker.is_alive()
        bridge.process_line(_request("computer.respond", valid, "right"))
        worker.join(timeout=2)
        assert not worker.is_alive() and not errors and result == upload()
        bridge.process_line(_request("computer.respond", valid, "duplicate"))
        assert next(line for line in _lines(out) if line.get("id") == "duplicate")["ok"] is False
    finally:
        job.cancellation_event.set()
        bridge._cancel_pending_computer_use(session, "test_end")
        worker.join(timeout=2)


def test_expired_status_reply_cannot_restore_a_pending_upload(tmp_path, monkeypatch):
    from alysis_code.ide import stdio_bridge as module

    monkeypatch.setattr(module, "DEFAULT_COMPUTER_USE_TIMEOUT_SECONDS", 0.05)
    bridge, out, session, job = _bridge_session(tmp_path)
    session.computer_use_host = replace(session.computer_use_host, uploads_available=True)
    result = {}

    def run():
        session.surface.with_job(job.job_id)
        session.surface.emit_tool_call_started(
            "upload-status-call", "computer_upload_status", "[redacted]"
        )
        result.update(
            bridge._request_computer_use(
                session, "computer_upload_status", {"upload_id": "upload-1"}
            )
        )

    worker = threading.Thread(target=run)
    worker.start()
    request = _wait_for_event(out, "computer_use_request")
    worker.join(timeout=2)
    assert not worker.is_alive() and result["error"] == "computer_use_timeout"
    bridge.process_line(
        _request("computer.respond", _respond_params(session, request, upload()), "late")
    )
    assert next(line for line in _lines(out) if line.get("id") == "late")["ok"] is False


def test_absent_upload_pair_refuses_before_any_host_request(tmp_path):
    bridge, out, session, job = _bridge_session(tmp_path)
    session.surface.with_job(job.job_id)
    session.surface.emit_tool_call_started("upload-call", "computer_upload_status", "[redacted]")
    result = bridge._request_computer_use(
        session, "computer_upload_status", {"upload_id": "upload-1"}
    )
    assert result["error"] == "computer_use_host_unavailable"
    assert not any(event.get("type") == "computer_use_request" for event in _lines(out))
