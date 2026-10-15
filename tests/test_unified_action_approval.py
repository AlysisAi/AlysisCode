"""Provider-free checks of the production engine's common action authority."""

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_approval_review import _reviewer, _Surface, _tools
from test_computer_use_approval import challenge
from test_ide_computer_use_host import _bridge_session

from alysis_code.agent.action_approval import decide_action_approval
from alysis_code.agent.errors import AgentRuntimeError, ApprovalReviewDeniedError
from alysis_code.mcp.manager import McpHostToolBinding
from alysis_code.surface import ApprovalDecision, ApprovalRequest, StandingApproval


def test_native_expiry_is_explicit_before_a_request_is_published(tmp_path: Path):
    bridge, _out, session, _job = _bridge_session(tmp_path)
    try:
        decision = bridge._request_approval(
            session,
            ApprovalRequest("computer_use", "Fixture", "Fixture"),
            lambda _: None,
            host_expires_at=datetime.now(UTC) - timedelta(seconds=1),
            host_deadline=time.monotonic() - 1,
        )
        assert decision.allow is False and decision.status == "expired"
        assert not session.pending_approvals
    finally:
        bridge.close()


@pytest.mark.parametrize("mode,asks", [("review", 1), ("auto", 1), ("fullaccess", 0)])
@pytest.mark.parametrize(
    "kind",
    ["shell_run", "fs_write", "mcp", "browser_click", "computer_use", "github.select_target"],
)
def test_modes_share_one_authority(tmp_path: Path, mode: str, asks: int, kind: str):
    surface = _Surface(allow=True)
    result = decide_action_approval(
        ApprovalRequest(kind, "fixture request", "exact fixture action"),
        mode=mode,
        root=tmp_path,
        surface=surface,
        can_ask_person=True,
    )
    assert result.allow
    assert len(surface.requests) == asks


def test_full_access_skips_mandatory_prompt_but_not_an_explicit_deny(tmp_path: Path):
    surface = _Surface(allow=False)
    request = ApprovalRequest(
        "computer_use", "fixture", "fixture", metadata={"mandatory_explicit_approval": True}
    )
    assert decide_action_approval(
        request, mode="fullaccess", root=tmp_path, surface=surface, can_ask_person=False
    ).allow
    surface.standing_approval = lambda _: StandingApproval("deny", "saved deny rule")
    assert not decide_action_approval(
        request, mode="fullaccess", root=tmp_path, surface=surface, can_ask_person=True
    ).allow
    assert surface.requests == []


def test_expiry_is_not_a_human_denial_and_never_dispatches(tmp_path: Path):
    surface = _Surface()
    surface.request_approval = lambda _: ApprovalDecision(False, status="expired")
    with pytest.raises(AgentRuntimeError) as caught:
        decide_action_approval(
            ApprovalRequest("mcp", "fixture", "fixture"),
            mode="review",
            root=tmp_path,
            surface=surface,
            can_ask_person=True,
        )
    assert caught.value.result_payload == {
        "error_code": "approval_expired",
        "dispatch_state": "not_dispatched",
        "recoverable": True,
    }


@pytest.mark.parametrize(
    "mode,allow,calls,asks",
    [
        ("fullaccess", False, 1, 0),
        ("review", True, 1, 1),
        ("review", False, 0, 1),
        ("auto", True, 1, 1),
        ("readonly", True, 0, 0),
    ],
)
def test_generic_mcp_uses_engine_admission(
    tmp_path: Path, mode: str, allow: bool, calls: int, asks: int
):
    dispatched = []
    binding = McpHostToolBinding(
        "update",
        "mcp__fixture__update",
        "Fixture update",
        {"type": "object"},
        lambda args: dispatched.append(args) or {"ok": True},
    )
    manager = SimpleNamespace(tool_bindings=[binding])
    surface = _Surface(allow=allow)
    tools = _tools(tmp_path, surface, mode=mode, reviewer=None, mcp_manager=manager)
    if mode == "readonly":
        assert binding.tool_alias not in tools
    elif allow or mode == "fullaccess":
        assert tools[binding.tool_alias].run({"value": "synthetic"}) == {"ok": True}
    else:
        with pytest.raises(AgentRuntimeError):
            tools[binding.tool_alias].run({"value": "synthetic"})
    assert len(dispatched) == calls
    assert len(surface.requests) == asks


@pytest.mark.parametrize(
    "verdict,calls,asks", [("allow", 1, 0), ("deny", 0, 0), ("ask_person", 1, 1)]
)
def test_mcp_uses_bound_reviewer_and_redacts_only_the_approval_preview(
    tmp_path: Path, verdict: str, calls: int, asks: int
):
    dispatched = []
    binding = McpHostToolBinding(
        "update",
        "mcp__fixture__update",
        "Fixture update",
        {"type": "object"},
        lambda args: dispatched.append(args) or {"ok": True},
    )
    reviewer, client, store = _reviewer(tmp_path, [{"decision": verdict, "reason": "Fixture"}])
    surface = _Surface(allow=True)
    tools = _tools(
        tmp_path,
        surface,
        mode="auto",
        reviewer=reviewer,
        store=store,
        mcp_manager=SimpleNamespace(tool_bindings=[binding]),
    )
    arguments = {"value": "synthetic", "password": "fixture-sensitive-value"}
    if verdict == "deny":
        with pytest.raises(ApprovalReviewDeniedError) as caught:
            tools[binding.tool_alias].run(arguments)
        assert caught.value.result_payload["dispatch_state"] == "not_dispatched"
    else:
        tools[binding.tool_alias].run(arguments)
    assert len(dispatched) == calls
    assert len(surface.requests) == asks
    assert len(client.calls) == 1
    assert "fixture-sensitive-value" not in str(client.calls)
    assert all("fixture-sensitive-value" not in request.preview for request in surface.requests)
    if dispatched:
        assert dispatched == [arguments], (
            "The redacted preview must not alter exact tool arguments."
        )


@pytest.mark.parametrize(
    "mode,tool,asks,executions",
    [
        ("fullaccess", "computer_action", 0, 1),
        ("review", "computer_action", 1, 1),
        ("auto", "computer_action", 1, 1),
        ("readonly", "computer_action", 0, 0),
        ("fullaccess", "computer_upload", 0, 1),
    ],
)
def test_native_and_chrome_preflight_obey_effective_engine_mode(
    tmp_path: Path, monkeypatch, mode: str, tool: str, asks: int, executions: int
):
    bridge, _out, session, _job = _bridge_session(tmp_path)
    session.mode = mode
    exchanges = []
    approvals = []

    def exchange(_session, _tool, args):
        exchanges.append(args)
        return (
            challenge(True)
            if args.get("approval_phase") == "prepare"
            else {"state": {"snapshot_id": "after"}}
        )

    monkeypatch.setattr(bridge, "_exchange_computer_use", exchange)
    monkeypatch.setattr(
        bridge,
        "_request_approval",
        lambda *args, **kwargs: approvals.append(args) or ApprovalDecision(True),
    )
    result = bridge._request_computer_use(
        session, tool, {"action": "key", "keys": ["Return"], "snapshot_id": "fixture"}
    )
    assert len(approvals) == asks
    assert sum(args.get("approval_phase") == "execute" for args in exchanges) == executions
    if mode == "readonly":
        assert result["dispatch_state"] == "not_dispatched"
    bridge.close()
