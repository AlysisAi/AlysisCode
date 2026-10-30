from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_ide_computer_use_host import _bridge_session, _lines

from alysis_code.ide.approval_binding import build_approval_binding
from alysis_code.surface.types import ApprovalDecision, ApprovalRequest

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")


def challenge(required: bool = True) -> dict[str, Any]:
    return {
        "approval_preflight": {
            "version": 1,
            "required": required,
            "token": "a" * 64,
            "scope_digest": "sha256:" + "b" * 64,
            "preview": "Send in fixture",
            "reason": "External side effect",
            "action_text": "synthetic input",
            "expires_at": (datetime.now(UTC) + timedelta(seconds=30))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
        }
    }


@pytest.mark.parametrize("allow", [True, False])
def test_action_is_suspended_until_bound_approval_and_resumes_only_on_allow(
    tmp_path: Path, monkeypatch, allow: bool
) -> None:
    bridge, out, session, job = _bridge_session(tmp_path)
    calls = []

    def exchange(_session, _tool, args):
        calls.append(dict(args))
        return (
            challenge()
            if args.get("approval_phase") == "prepare"
            else {"state": {"snapshot_id": "next"}}
        )

    monkeypatch.setattr(bridge, "_exchange_computer_use", exchange)

    def approve(_session, request, emit, **_kwargs):
        assert len(calls) == 1
        assert request.kind == "computer_use"
        assert request.allow_for_session_scope["job_id"] == job.job_id
        assert request.metadata["arguments_digest"]
        return ApprovalDecision(allow=allow)

    monkeypatch.setattr(bridge, "_request_approval", approve)
    result = bridge._request_computer_use(
        session,
        "computer_action",
        {"action": "press", "element_id": "send", "snapshot_id": "fresh"},
    )
    assert len(calls) == (2 if allow else 1)
    if allow:
        assert calls[1]["approval_token"] == "a" * 64
        assert result == {"state": {"snapshot_id": "next"}}
    else:
        assert result["error"] == "computer_use_approval_denied"


def test_nonconforming_host_cannot_skip_engine_preflight(tmp_path: Path, monkeypatch) -> None:
    bridge, out, session, job = _bridge_session(tmp_path)
    monkeypatch.setattr(
        bridge, "_exchange_computer_use", lambda *args: {"state": {"snapshot_id": "fake"}}
    )
    result = bridge._request_computer_use(session, "computer_action", {"action": "press"})
    assert result["error"] == "approval_preflight_required"


def test_return_is_gated_even_when_host_reports_no_risk(tmp_path: Path, monkeypatch) -> None:
    bridge, out, session, job = _bridge_session(tmp_path)
    monkeypatch.setattr(bridge, "_exchange_computer_use", lambda *args: challenge(False))
    approvals = []

    def deny(*args, **_kwargs):
        approvals.append(args)
        return ApprovalDecision(allow=False)

    monkeypatch.setattr(bridge, "_request_approval", deny)
    assert (
        bridge._request_computer_use(
            session, "computer_action", {"action": "key", "keys": ["Return"]}
        )["error"]
        == "computer_use_approval_denied"
    )
    assert len(approvals) == 1


def test_real_approval_emits_required_and_times_out_without_dispatch(
    tmp_path: Path, monkeypatch
) -> None:
    bridge, out, session, job = _bridge_session(tmp_path)
    bridge._approval_timeout_seconds = 0.01
    calls = []
    monkeypatch.setattr(
        bridge, "_exchange_computer_use", lambda *args: calls.append(args) or challenge()
    )
    result = bridge._request_computer_use(session, "computer_action", {"action": "press"})
    assert result["error"] == "computer_use_approval_expired"
    assert len(calls) == 1
    required = next(event for event in _lines(out) if event["type"] == "approval.required")
    assert required["payload"]["action_fingerprint"]
    assert required["payload"]["expires_at"]
    assert not session.pending_approvals
    assert not bridge._event_buffers.get(session.session_id)


def test_turn_scope_never_matches_another_job_target_or_action(tmp_path: Path) -> None:
    from alysis_code.ide.stdio_bridge import ApprovalScopeRecord, _approved_scope_matches

    scope = {
        "type": "computer_use_turn_action",
        "job_id": "j1",
        "scope_digest": "target1",
        "arguments_digest": "text1",
    }
    approved = [ApprovalScopeRecord(kind="computer_use", scope=scope, key="key")]
    assert _approved_scope_matches(approved, "computer_use", dict(scope))
    for key in ("job_id", "scope_digest", "arguments_digest"):
        assert not _approved_scope_matches(approved, "computer_use", {**scope, key: "changed"})


def test_display_redaction_cannot_collapse_exact_action_binding(tmp_path: Path) -> None:
    def fingerprint(digest):
        request = ApprovalRequest(
            kind="computer_use",
            reason="test",
            preview="redacted text",
            metadata={"arguments_digest": digest, "scope_digest": "bound"},
        )
        return build_approval_binding(
            request, project_root=tmp_path, runtime_fingerprint="r", mode="review"
        ).action_fingerprint

    assert fingerprint("a" * 64) != fingerprint("b" * 64)


def test_real_allow_turn_decision_requires_all_fingerprints_and_resumes_exact_request(
    tmp_path: Path, monkeypatch
) -> None:
    from test_ide_computer_use_host import _request, _wait_for_event

    bridge, out, session, job = _bridge_session(tmp_path)
    calls = []

    def exchange(_session, _tool, args):
        calls.append(args)
        return (
            challenge()
            if args["approval_phase"] == "prepare"
            else {"state": {"snapshot_id": "after"}}
        )

    monkeypatch.setattr(bridge, "_exchange_computer_use", exchange)
    result = {}

    def run():
        session.surface.with_job(job.job_id)
        session.surface.emit_tool_call_started("call", "computer_action", "[redacted]")
        result.update(
            bridge._request_computer_use(
                session, "computer_action", {"action": "press", "snapshot_id": "s"}
            )
        )

    worker = threading.Thread(target=run)
    worker.start()
    required = _wait_for_event(out, "approval.required")["payload"]
    params = {
        "session_id": session.session_id,
        "approval_id": required["approval_id"],
        "allow": True,
        "allow_for_session": True,
    }
    bridge.process_line(_request("approval.respond", params, "bad"))
    assert any(
        line.get("error", {}).get("code") == "approval_binding_mismatch" for line in _lines(out)
    )
    assert len(calls) == 1
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
        params[field] = required[field]
    bridge.process_line(_request("approval.respond", params, "good"))
    worker.join(2)
    assert not worker.is_alive()
    assert result == {"state": {"snapshot_id": "after"}}
    assert len(calls) == 2
    assert session.approved_approval_scopes[0].scope["job_id"] == job.job_id


def test_workflow_schema_drift_revokes_snapshot_authority() -> None:
    from alysis_code.agent.computer_use_tools import build_computer_use_tools

    tools = build_computer_use_tools(
        lambda *args: {"snapshot_id": "opaque", "alysis_workflow": {"version": 99}}
    )
    assert tools["computer_state"].run({})["error"] == "invalid_host_workflow"
    assert (
        tools["computer_action"].run({"action": "press", "element_id": "x"})["error"]
        == "computer_use_fresh_observation_required"
    )
