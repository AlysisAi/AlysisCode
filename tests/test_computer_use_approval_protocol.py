from __future__ import annotations

import copy
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from test_computer_use_approval import challenge
from test_ide_computer_use_host import _bridge_session, _lines, _request, _wait_for_event

from alysis_code.agent.computer_use_tools import COMPUTER_USE_SYSTEM_PROMPT
from alysis_code.ide.stdio_bridge import PendingComputerUseRequest


def _respond_params(session: Any, event: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    payload = event["payload"]
    return {
        **{
            key: payload[key]
            for key in (
                "request_id",
                "response_nonce",
                "operation_id",
                "tool_call_id",
                "tool_name",
                "arguments_fingerprint",
                "expires_at",
            )
        },
        "session_id": session.session_id,
        "job_id": event["job_id"],
        "ok": True,
        "result": result,
    }


def _wait_execute(out: Any) -> dict[str, Any]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        for event in _lines(out):
            if (
                event.get("type") == "computer_use_request"
                and event["payload"]["arguments"].get("approval_phase") == "execute"
            ):
                return event
        time.sleep(0.01)
    raise AssertionError("No bound execution followed the accepted preflight")


@pytest.mark.parametrize("required", [False, True])
def test_real_dispatch_accepts_bound_host_preflight_then_approves_and_executes(
    tmp_path: Path, required: bool
) -> None:
    bridge, out, session, job = _bridge_session(tmp_path)
    result: dict[str, Any] = {}
    errors: list[BaseException] = []

    def run() -> None:
        session.surface.with_job(job.job_id)
        session.surface.emit_tool_call_started("fixture-call", "computer_action", "[redacted]")
        try:
            result.update(
                bridge._request_computer_use(
                    session,
                    "computer_action",
                    {"action": "press", "element_id": "fixture", "snapshot_id": "snapshot"},
                )
            )
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        prepare = _wait_for_event(out, "computer_use_request")
        assert prepare["payload"]["arguments"]["approval_phase"] == "prepare"
        prepared_response = _respond_params(session, prepare, challenge(required))
        bridge.process_line(_request("computer.respond", prepared_response, "prepared"))
        assert next(line for line in _lines(out) if line.get("id") == "prepared")["ok"] is True
        if session.mode != "fullaccess":
            approval = _wait_for_event(out, "approval.required")["payload"]
            assert not any(
                event.get("type") == "computer_use_request"
                and event["payload"]["arguments"].get("approval_phase") == "execute"
                for event in _lines(out)
            )
            decision = {
                "session_id": session.session_id,
                "approval_id": approval["approval_id"],
                "allow": True,
                "allow_for_session": False,
            }
            for field in (
                "action_fingerprint",
                "project_fingerprint",
                "runtime_fingerprint",
                "policy_fingerprint",
                "operation_id",
                "job_id",
                "tool_call_id",
                "expires_at",
            ):
                decision[field] = approval[field]
            bridge.process_line(_request("approval.respond", decision, "approved"))
            assert next(line for line in _lines(out) if line.get("id") == "approved")["ok"] is True
        execute = _wait_execute(out)
        assert execute["payload"]["arguments"]["approval_token"] == "a" * 64
        assert execute["payload"]["arguments"]["element_id"] == "fixture"
        assert execute["payload"]["arguments"]["snapshot_id"] == "snapshot"
        bridge.process_line(
            _request(
                "computer.respond",
                _respond_params(session, execute, {"state": {"snapshot_id": "after"}}),
                "executed",
            )
        )
        worker.join(2)
        assert not worker.is_alive()
        assert not errors
        assert result == {"state": {"snapshot_id": "after"}}
        assert prepared_response["result"]["approval_preflight"]["token"] == "a" * 64
        assert "a" * 64 not in str(bridge._event_buffers.get(session.session_id, ()))
    finally:
        job.cancellation_event.set()
        bridge._cancel_pending_computer_use(session, "fixture_cleanup")
        bridge._cancel_pending_approvals(session, "fixture_cleanup")
        worker.join(2)
        assert not worker.is_alive()


def _pending_fixture(tmp_path: Path) -> tuple[Any, Any, Any, Any, dict[str, Any]]:
    bridge, out, session, job = _bridge_session(tmp_path)
    pending = PendingComputerUseRequest(
        request_id="fixture-request",
        response_nonce="nonce",
        session_id=session.session_id,
        operation_id="operation-1",
        job_id=job.job_id,
        tool_call_id="call",
        tool_name="computer_action",
        arguments_fingerprint="sha256:" + "c" * 64,
        expires_at="2026-09-08T12:00:00.000Z",
        deadline=time.monotonic() + 30,
        approval_preflight_expected=True,
    )
    session.pending_computer_use[pending.request_id] = pending
    params = {
        key: getattr(pending, key)
        for key in (
            "session_id",
            "request_id",
            "response_nonce",
            "operation_id",
            "job_id",
            "tool_call_id",
            "tool_name",
            "arguments_fingerprint",
            "expires_at",
        )
    }
    params.update(ok=True, result=challenge(False))
    return bridge, out, session, pending, params


@pytest.mark.parametrize(
    "variant",
    [
        "neighbor",
        "nested_neighbor",
        "outer_neighbor",
        "top_neighbor",
        "malformed_token",
        "wrong_scope",
        "wrong_version",
        "wrong_required",
        "not_prepare",
        "state",
        "failed",
        "other_method",
    ],
)
def test_preflight_capability_exception_does_not_admit_other_secrets(
    tmp_path: Path, variant: str
) -> None:
    bridge, out, _session, pending, params = _pending_fixture(tmp_path)
    method = "computer.respond"
    preflight = params["result"]["approval_preflight"]
    if variant == "neighbor":
        preflight["api_key"] = "private-fixture"
    elif variant == "nested_neighbor":
        preflight["extra"] = [{"access_token": "private-fixture"}]
    elif variant == "outer_neighbor":
        params["result"]["password"] = "private-fixture"
    elif variant == "top_neighbor":
        params["credential"] = "private-fixture"
    elif variant == "malformed_token":
        preflight["token"] = "private-fixture"
    elif variant == "wrong_scope":
        preflight["scope_digest"] = "invalid"
    elif variant == "wrong_version":
        preflight["version"] = True
    elif variant == "wrong_required":
        preflight["required"] = "false"
    elif variant == "not_prepare":
        pending.approval_preflight_expected = False
    elif variant == "state":
        pending.tool_name = params["tool_name"] = "computer_state"
    elif variant == "failed":
        params["ok"] = False
    else:
        method = "health"
    original = copy.deepcopy(params)
    bridge.process_line(_request(method, params, "rejected"))
    response = next(line for line in _lines(out) if line.get("id") == "rejected")
    assert response["ok"] is False
    assert response["error"]["code"] == "inline_secret_rejected"
    assert not pending.resolved
    assert pending.response is None
    assert params == original


@pytest.mark.parametrize(
    ("variant", "code"),
    [
        ("binding", "computer_use_binding_mismatch"),
        ("expired", "expired_computer_request"),
        ("resolved", "duplicate_response"),
        ("unknown", "unknown_computer_request"),
    ],
)
def test_preflight_token_cannot_bypass_pending_request_authority(
    tmp_path: Path, variant: str, code: str
) -> None:
    bridge, out, session, pending, params = _pending_fixture(tmp_path)
    if variant == "binding":
        params["response_nonce"] = "wrong"
    elif variant == "expired":
        pending.deadline = time.monotonic() - 1
    elif variant == "resolved":
        pending.resolved = True
    else:
        session.pending_computer_use.clear()
    bridge.process_line(_request("computer.respond", params, "rejected"))
    response = next(line for line in _lines(out) if line.get("id") == "rejected")
    assert response["ok"] is False
    assert response["error"]["code"] == code
    assert pending.response is None or pending.response.get("error") == "computer_use_timeout"


def test_computer_use_prompt_allows_read_only_record_navigation_without_setting_changes() -> None:
    assert (
        "observed same-origin account-settings or purchase-history navigation link solely"
        in COMPUTER_USE_SYSTEM_PROMPT
    )
    assert "to read the records the user requested" in COMPUTER_USE_SYSTEM_PROMPT
    assert (
        "Never change security/privacy settings or payment/subscription details"
        in COMPUTER_USE_SYSTEM_PROMPT
    )
    assert "authentication bypass" in COMPUTER_USE_SYSTEM_PROMPT
