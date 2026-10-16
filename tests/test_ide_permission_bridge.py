from __future__ import annotations

import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from alysis_code.config import AppConfig
from alysis_code.ide import stdio_bridge
from alysis_code.ide.prompt_queue import DurablePromptQueue
from alysis_code.ide.stdio_bridge import ApprovalScopeRecord, StdioBridge
from alysis_code.permission_policy import PermissionPolicyError, PermissionPolicyStore
from alysis_code.surface.types import ApprovalRequest


def _request(method: str, params: dict[str, Any], request_id: str) -> str:
    return (
        json.dumps(
            {
                "protocol_version": "1",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        + "\n"
    )


def _response(output: io.StringIO, request_id: str) -> dict[str, Any]:
    return next(
        json.loads(line)
        for line in output.getvalue().splitlines()
        if json.loads(line).get("id") == request_id
    )


def test_permission_rule_protocol_is_explainable_and_redacts_commands(tmp_path: Path) -> None:
    output = io.StringIO()
    bridge = StdioBridge(
        stdout=output,
        permission_policy_store=PermissionPolicyStore(tmp_path / "permission.json"),
        prompt_queue=DurablePromptQueue(tmp_path / "queue.sqlite3"),
    )

    bridge.process_line(
        _request(
            "permission.rules.grant",
            {
                "effect": "allow",
                "tool_pattern": "shell_run",
                "command_pattern": "pytest tests/*",
                "confirm": True,
            },
            "grant",
        )
    )
    granted = _response(output, "grant")["result"]["rule"]
    assert granted["has_command_pattern"] is True
    assert "command_pattern" not in granted

    bridge.process_line(_request("permission.rules.list", {}, "list"))
    listed = _response(output, "list")["result"]
    assert listed["command_patterns_redacted"] is True
    assert "pytest" not in json.dumps(listed)

    bridge.process_line(
        _request(
            "permission.evaluate",
            {"tool_name": "shell_run", "command": "pytest tests/unit"},
            "evaluate",
        )
    )
    evaluation = _response(output, "evaluate")["result"]
    assert evaluation["decision"] == "allow"
    assert evaluation["matched_rule_id"] == granted["id"]
    assert evaluation["reason"] == "matched_rule"

    bridge.process_line(
        _request(
            "permission.rules.revoke",
            {"rule_id": granted["id"], "yes": True},
            "revoke",
        )
    )
    assert _response(output, "revoke")["result"]["status"] == "revoked"
    bridge.close()


def test_permission_sensitive_override_and_session_grant_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))

    class FakeSession:
        store = SimpleNamespace(session_artifact_root=tmp_path / "artifacts")

        def close(self) -> None:
            return

    output = io.StringIO()
    store = PermissionPolicyStore(tmp_path / "permission.json")
    store.grant("allow", tool_pattern="fs_write")
    bridge = StdioBridge(
        stdout=output,
        create_session_fn=lambda **_kwargs: FakeSession(),
        permission_policy_store=store,
        prompt_queue=DurablePromptQueue(tmp_path / "queue.sqlite3"),
    )
    bridge.process_line(
        _request(
            "session.create",
            {
                "workspace": os.fspath(workspace),
                "mode": "review",
                "model": "test-model",
                "session_id": "permission-session",
            },
            "create",
        )
    )

    bridge.process_line(
        _request(
            "permission.evaluate",
            {
                "tool_name": "fs_write",
                "workspace": os.fspath(workspace),
                "paths": [".env"],
            },
            "sensitive",
        )
    )
    assert _response(output, "sensitive")["result"] == {
        "decision": "ask",
        "reason": "sensitive_resource_requires_approval",
        "matched_rule_id": "override:sensitive",
        "matched_rule_source": "builtin_safety",
        "specificity": 2_147_483_647,
    }

    assert "error" not in _response(output, "create"), _response(output, "create")
    session = bridge._sessions["permission-session"]
    session.approved_approval_scopes.append(
        ApprovalScopeRecord(
            kind="fs_write",
            scope={"type": "exact_file_set", "kind": "fs_write", "files": ["safe.txt"]},
            key="test-key",
        )
    )
    bridge.process_line(
        _request("permission.session.list", {"session_id": session.session_id}, "grants")
    )
    grants = _response(output, "grants")["result"]["grants"]
    assert len(grants) == 1
    assert "files" not in json.dumps(grants)

    bridge.process_line(
        _request(
            "permission.session.revoke",
            {"session_id": session.session_id, "grant_id": grants[0]["id"]},
            "session-revoke",
        )
    )
    assert _response(output, "session-revoke")["result"]["status"] == "revoked"
    assert session.approved_approval_scopes == []
    bridge.close()


def test_permission_evaluate_detects_workspace_symlink_escape(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    link = workspace / "linked"
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")

    output = io.StringIO()
    store = PermissionPolicyStore(tmp_path / "permission.json")
    store.grant("allow", tool_pattern="fs_write", path_pattern="linked/**")
    bridge = StdioBridge(
        stdout=output,
        permission_policy_store=store,
        prompt_queue=DurablePromptQueue(tmp_path / "queue.sqlite3"),
    )

    bridge.process_line(
        _request(
            "permission.evaluate",
            {
                "tool_name": "fs_write",
                "workspace": os.fspath(workspace),
                "paths": ["linked/output.txt"],
            },
            "symlink-escape",
        )
    )

    result = _response(output, "symlink-escape")["result"]
    assert result["decision"] == "ask"
    assert result["reason"] == "external_directory_requires_approval"
    assert result["matched_rule_id"] == "override:external_directory"
    bridge.close()


@pytest.fixture
def approval_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='approval-fixture'\n")
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))
    fake = SimpleNamespace(
        store=SimpleNamespace(session_artifact_root=tmp_path / "artifacts"), close=lambda: None
    )
    store = PermissionPolicyStore(tmp_path / "permission.json")
    bridge = StdioBridge(
        stdout=io.StringIO(),
        create_session_fn=lambda **_kwargs: fake,
        permission_policy_store=store,
        prompt_queue=DurablePromptQueue(tmp_path / "queue.sqlite3"),
    )
    bridge.process_line(
        _request(
            "session.create",
            {
                "workspace": os.fspath(workspace),
                "mode": "review",
                "session_id": "approval-session",
            },
            "create",
        )
    )
    try:
        yield bridge, bridge._sessions["approval-session"], store
    finally:
        bridge.close()


@pytest.mark.parametrize(
    "path,metadata,ask_rule",
    [
        (".env", {}, False),
        ("../external.txt", {}, False),
        ("safe.txt", {"mandatory_explicit_approval": True}, False),
        ("safe.txt", {"allow_for_session_disabled": True}, False),
        ("safe.txt", {}, True),
    ],
)
def test_one_time_safety_and_explicit_ask_override_cached_grants(
    approval_bridge,
    path: str,
    metadata: dict,
    ask_rule: bool,
) -> None:
    bridge, session, store = approval_bridge
    request = ApprovalRequest(
        kind="fs_write", reason="write", preview="write fixture", files=[path], metadata=metadata
    )
    scope, key, _ = stdio_bridge._approval_scope_for_request("fs_write", request)
    session.approved_approval_scopes.append(
        ApprovalScopeRecord(kind="fs_write", scope=scope, key=key)
    )
    if ask_rule:
        store.grant("ask", tool_pattern="fs_write", path_pattern=path)
    prompts = []

    def respond(event):
        if getattr(event, "payload", {}).get("kind") != "approval":
            return
        prompts.append(event.payload)
        previous_policy = stdio_bridge.approval_policy_fingerprint(
            mode=session.mode,
            kind="fs_write",
            destructive=False,
            allow_for_session_supported=True,
        )
        assert event.payload["policy_fingerprint"] != previous_policy
        with pytest.raises(stdio_bridge.ProtocolError) as mismatch:
            bridge._resolve_approval(
                session=session,
                approval_id=event.payload["approval_id"],
                allow=True,
                allow_for_session=True,
                response_bindings={**event.payload, "policy_fingerprint": previous_policy},
                request_id="previous-policy",
            )
        assert mismatch.value.code == "approval_binding_mismatch"
        bridge._resolve_approval(
            session=session,
            approval_id=event.payload["approval_id"],
            allow=False,
            allow_for_session=False,
            response_bindings=event.payload,
            request_id="decision",
        )

    decision = bridge._request_approval(session, request, respond)
    assert decision.allow is False
    assert len(prompts) == 1
    assert prompts[0]["allow_for_session_supported"] is False


@pytest.mark.parametrize("kind,path", [("fs_read", ".env"), ("fs_delete", "safe.txt")])
def test_sensitive_approval_cannot_create_a_session_grant(approval_bridge, kind, path) -> None:
    bridge, session, _ = approval_bridge
    prompts = []

    def respond(event):
        if getattr(event, "payload", {}).get("kind") == "approval":
            prompts.append(event.payload)
            bridge._resolve_approval(
                session=session,
                approval_id=event.payload["approval_id"],
                allow=True,
                allow_for_session=True,
                response_bindings=event.payload,
                request_id="decision",
            )

    request = ApprovalRequest(
        kind=kind, reason="approval", preview="sensitive or destructive fixture", files=[path]
    )
    for _ in range(2):
        decision = bridge._request_approval(session, request, respond)
        assert decision.allow and not decision.allow_for_session
    assert len(prompts) == 2
    assert all(prompt["allow_for_session_supported"] is False for prompt in prompts)
    assert session.approved_approval_scopes == []


def test_revoked_grant_is_not_resurrected_by_approval_waiter(approval_bridge) -> None:
    bridge, session, _ = approval_bridge

    def respond(event):
        if getattr(event, "payload", {}).get("kind") == "approval":
            bridge._resolve_approval(
                session=session,
                approval_id=event.payload["approval_id"],
                allow=True,
                allow_for_session=True,
                response_bindings=event.payload,
                request_id="decision",
            )
            grants = bridge._permission_session_list(
                stdio_bridge.ProtocolRequest(
                    protocol_version="1",
                    id="list",
                    method="permission.session.list",
                    params={"session_id": session.session_id},
                )
            )["grants"]
            bridge.process_line(
                _request(
                    "permission.session.revoke",
                    {"session_id": session.session_id, "grant_id": grants[0]["id"]},
                    "revoke",
                )
            )

    decision = bridge._request_approval(
        session,
        ApprovalRequest(kind="fs_write", reason="write", preview="fixture", files=["safe.txt"]),
        respond,
    )
    assert decision.allow
    assert session.approved_approval_scopes == []


def test_policy_failure_denies_even_a_cached_session_grant(
    approval_bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, session, store = approval_bridge
    request = ApprovalRequest(
        kind="fs_write", reason="write", preview="fixture", files=["safe.txt"]
    )
    scope, key, _ = stdio_bridge._approval_scope_for_request("fs_write", request)
    session.approved_approval_scopes.append(
        ApprovalScopeRecord(kind="fs_write", scope=scope, key=key)
    )

    def fail(_request):
        raise PermissionPolicyError("invalid policy request")

    monkeypatch.setattr(store, "evaluate", fail)
    events = []
    decision = bridge._request_approval(session, request, events.append)
    assert not decision.allow
    assert not any(getattr(event, "payload", {}).get("kind") == "approval" for event in events)


def test_standing_approval_applies_the_rules_the_card_would(
    approval_bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    # "Approve for me" consults these before its reviewer; a person's rules always win.
    bridge, session, store = approval_bridge
    command = ApprovalRequest(
        kind="shell_run", reason="r", preview="make build", command="make build"
    )
    assert bridge._standing_approval(session, command) is None

    write = ApprovalRequest(kind="fs_write", reason="w", preview="p", files=["safe.txt"])
    scope, key, _ = stdio_bridge._approval_scope_for_request("fs_write", write)
    session.approved_approval_scopes.append(
        ApprovalScopeRecord(kind="fs_write", scope=scope, key=key)
    )
    assert bridge._standing_approval(session, write).outcome == "allow"

    for path in (".env", "../external.txt"):
        sensitive = ApprovalRequest(kind="fs_write", reason="w", preview="p", files=[path])
        assert bridge._standing_approval(session, sensitive).outcome == "ask_person"

    rule = store.grant("deny", tool_pattern="shell_run", command_pattern="make build")
    assert bridge._standing_approval(session, command).outcome == "deny"
    store.revoke(rule.id)
    rule = store.grant("ask", tool_pattern="shell_run", command_pattern="make build")
    assert bridge._standing_approval(session, command).outcome == "ask_person"
    store.revoke(rule.id)
    store.grant("allow", tool_pattern="shell_run", command_pattern="make build")
    assert bridge._standing_approval(session, command).outcome == "allow"

    def fail(_request):
        raise PermissionPolicyError("invalid policy request")

    monkeypatch.setattr(store, "evaluate", fail)
    assert bridge._standing_approval(session, command).outcome == "deny"
