from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from alysis_code.config import AppConfig
from alysis_code.host_actions import HostActionError
from alysis_code.ide import stdio_bridge
from alysis_code.ide.stdio_bridge import StdioBridge
from alysis_code.permission_policy import PermissionPolicyStore
from alysis_code.surface import ApprovalRequest


def _request(method: str, params: dict[str, Any], request_id: str) -> str:
    return (
        json.dumps(
            {
                "protocol_version": "1",
                "id": request_id,
                "method": method,
                "params": params,
            },
            sort_keys=True,
        )
        + "\n"
    )


def _lines(out: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def _wait_for(out: io.StringIO, predicate: Any, *, timeout: float = 2.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for line in _lines(out):
            if predicate(line):
                return line
        time.sleep(0.01)
    raise AssertionError("timed out waiting for protocol line")


def _response_for(out: io.StringIO, request_id: str) -> dict[str, Any]:
    return _wait_for(out, lambda line: line.get("id") == request_id)


def _make_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    workspace_trusted: bool = True,
    actions: tuple[str, ...] = ("tasks.list",),
    action: str = "tasks.list",
    arguments: dict[str, Any] | None = None,
    timeout: float = 1.0,
    approval_protocol: int = 0,
    mode: str = "auto",
    explicit_github_all: bool = False,
) -> tuple[io.StringIO, StdioBridge, str, dict[str, Any], list[Any], list[HostActionError]]:
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))
    captured: dict[str, Any] = {}
    results: list[Any] = []
    errors: list[HostActionError] = []

    class FakeStore:
        session_artifact_root = artifact_root

    class FakeSession:
        store = FakeStore()

        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            self.surface = kwargs["surface"]

        def run_turn(self, _message: str) -> int:
            handler = captured.get("host_action_handler")
            assert callable(handler)
            try:
                results.append(handler(action, dict(arguments or {})))
            except HostActionError as exc:
                errors.append(exc)
            self.surface.emit_message_end("done")
            return 0

        def close(self) -> None:
            return

    bridge = StdioBridge(
        stdout=out,
        create_session_fn=lambda **kwargs: FakeSession(**kwargs),
        host_action_timeout_seconds=timeout,
    )
    github_params = {}
    if explicit_github_all:
        from alysis_code.extensions.install import install_plugin
        from tests.fixtures.github_repository_policy import explicit_all

        install_plugin(source="alysis.github", repo_root=tmp_path, trust_prompt=lambda _: True)
        github_params["github_repository_policy"] = explicit_all(tmp_path)
    bridge.process_line(
        _request(
            "session.create",
            {
                "workspace": os.fspath(tmp_path),
                **github_params,
                "mode": mode,
                "model": "test-model",
                "workspace_trusted": workspace_trusted,
                "host_capabilities": {
                    "protocol_version": "1",
                    "approval_protocol": approval_protocol,
                    "actions": list(actions),
                },
            },
            "create",
        )
    )
    create = _response_for(out, "create")
    assert create["ok"] is True
    return (
        out,
        bridge,
        create["result"]["session_id"],
        captured,
        results,
        errors,
    )


def _start_host_action(out: io.StringIO, bridge: StdioBridge, session_id: str) -> dict[str, Any]:
    bridge.process_line(_request("chat.send", {"session_id": session_id, "message": "run"}, "chat"))
    return _wait_for(out, lambda line: line.get("type") == "host_action_requested")


def _respond(
    bridge: StdioBridge,
    event: dict[str, Any],
    *,
    request_id: str,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    workspace_fence: str | None = None,
    capability_fingerprint: str | None = None,
) -> None:
    payload = event["payload"]
    params: dict[str, Any] = {
        "session_id": event["session_id"],
        "host_action_id": payload["host_action_id"],
        "workspace_fence": workspace_fence or payload["workspace_fence"],
        "capability_fingerprint": capability_fingerprint or payload["capability_fingerprint"],
        "ok": error is None,
    }
    if error is None:
        params["result"] = result or {}
    else:
        params["error"] = error
    bridge.process_line(_request("host.action.respond", params, request_id))


def test_host_action_round_trip_is_workspace_and_capability_fenced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, session_id, captured, results, errors = _make_bridge(tmp_path, monkeypatch)
    event = _start_host_action(out, bridge, session_id)
    payload = event["payload"]

    assert captured["host_action_capabilities"] == frozenset({"tasks.list"})
    assert payload["action"] == "tasks.list"
    assert payload["arguments"] == {}
    assert Path(payload["workspace_root"]).resolve() == tmp_path.resolve()
    assert payload["max_result_bytes"] == 64 * 1024

    _respond(
        bridge,
        event,
        request_id="wrong-fence",
        workspace_fence="wf_00000000000000000000000000000000",
        result={"tasks": [], "truncated": False},
    )
    wrong = _response_for(out, "wrong-fence")
    assert wrong["ok"] is False
    assert wrong["error"]["code"] == "host_action_workspace_fence_mismatch"

    _respond(
        bridge,
        event,
        request_id="host-response",
        result={"tasks": [{"id": "task-1", "label": "Test"}], "truncated": False},
    )
    ack = _response_for(out, "host-response")
    _wait_for(out, lambda line: line.get("type") == "message_end")

    assert ack["ok"] is True
    assert ack["result"] == {
        "status": "applied",
        "session_id": session_id,
        "host_action_id": payload["host_action_id"],
        "action": "tasks.list",
        "outcome": "result",
    }
    assert results == [{"tasks": [{"id": "task-1", "label": "Test"}], "truncated": False}]
    assert errors == []
    bridge.close()


def test_host_action_response_rejects_capability_fence_and_extra_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, session_id, _captured, _results, _errors = _make_bridge(tmp_path, monkeypatch)
    event = _start_host_action(out, bridge, session_id)

    _respond(
        bridge,
        event,
        request_id="wrong-capability-fence",
        capability_fingerprint="0" * 64,
        result={"tasks": [], "truncated": False},
    )
    wrong = _response_for(out, "wrong-capability-fence")
    assert wrong["ok"] is False
    assert wrong["error"]["code"] == "host_action_capability_fence_mismatch"

    payload = event["payload"]
    bridge.process_line(
        _request(
            "host.action.respond",
            {
                "session_id": session_id,
                "host_action_id": payload["host_action_id"],
                "workspace_fence": payload["workspace_fence"],
                "capability_fingerprint": payload["capability_fingerprint"],
                "ok": True,
                "result": {"tasks": [], "truncated": False},
                "unexpected": True,
            },
            "extra-field",
        )
    )
    extra = _response_for(out, "extra-field")
    assert extra["ok"] is False
    assert extra["error"]["code"] == "invalid_host_action_response"

    _respond(
        bridge,
        event,
        request_id="valid-after-rejections",
        result={"tasks": [], "truncated": False},
    )
    assert _response_for(out, "valid-after-rejections")["ok"] is True
    _wait_for(out, lambda line: line.get("type") == "message_end")
    bridge.close()


def test_untrusted_workspace_drops_all_effective_host_capabilities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, _session_id, captured, _results, _errors = _make_bridge(
        tmp_path,
        monkeypatch,
        workspace_trusted=False,
        actions=("tasks.list", "tasks.run", "debug.list"),
    )
    created = _response_for(out, "create")["result"]

    assert created["host_actions"]["workspace_trusted"] is False
    assert created["host_actions"]["actions"] == []
    assert captured["host_action_handler"] is None
    assert captured["host_action_capabilities"] == frozenset()
    bridge.close()


def test_idle_session_close_emits_exact_host_execution_cleanup_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, session_id, _captured, _results, _errors = _make_bridge(
        tmp_path,
        monkeypatch,
        actions=("tasks.run", "tasks.status", "tasks.terminate"),
    )
    negotiated = _response_for(out, "create")["result"]["host_actions"]

    bridge.process_line(
        _request(
            "session.cancel",
            {"session_id": session_id, "reason": "new_session_requested"},
            "close",
        )
    )

    assert _response_for(out, "close")["result"]["state"] == "closed"
    closed = _wait_for(out, lambda line: line.get("type") == "session_closed")
    assert closed["session_id"] == session_id
    assert closed["payload"] == {
        "protocol_version": "1",
        "workspace_fence": negotiated["workspace_fence"],
        "capability_fingerprint": negotiated["capability_fingerprint"],
    }
    bridge.close()


def test_mandatory_host_execution_approval_ignores_persistent_allow_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))
    decisions: list[bool] = []

    class FakeStore:
        session_artifact_root = artifact_root

    class FakeSession:
        store = FakeStore()

        def __init__(self, **kwargs: Any) -> None:
            self.surface = kwargs["surface"]

        def run_turn(self, _message: str) -> int:
            decision = self.surface.request_approval(
                ApprovalRequest(
                    kind="ide_task_run",
                    reason="opaque IDE task may execute commands",
                    preview=f"workspace: {tmp_path.resolve()}\ntask_id: task-1",
                    metadata={
                        "mandatory_explicit_approval": True,
                        "allow_for_session_disabled": True,
                    },
                    allow_for_session_scope=None,
                )
            )
            decisions.append(decision.allow)
            self.surface.emit_message_end("done")
            return 0

        def close(self) -> None:
            return

    permission_store = PermissionPolicyStore(tmp_path / "permissions.json")
    permission_store.grant("allow", tool_pattern="ide_task_run")
    bridge = StdioBridge(
        stdout=out,
        create_session_fn=lambda **kwargs: FakeSession(**kwargs),
        permission_policy_store=permission_store,
        approval_timeout_seconds=1.0,
    )
    bridge.process_line(
        _request(
            "session.create",
            {"workspace": os.fspath(tmp_path), "mode": "auto", "model": "test-model"},
            "mandatory-create",
        )
    )
    session_id = _response_for(out, "mandatory-create")["result"]["session_id"]
    bridge.process_line(
        _request("chat.send", {"session_id": session_id, "message": "run task"}, "mandatory-chat")
    )

    approval = _wait_for(
        out,
        lambda line: (
            line.get("type") == "prompt_for_input"
            and line.get("payload", {}).get("kind") == "approval"
        ),
    )
    assert approval["payload"]["approval_kind"] == "ide_task_run"
    assert approval["payload"]["allow_for_session_supported"] is False
    assert not any(
        line.get("type") == "info_emitted"
        and "approval_auto_allowed" in str(line.get("payload", {}).get("message", ""))
        for line in _lines(out)
    )

    bridge.process_line(
        _request(
            "approval.respond",
            {
                "session_id": session_id,
                "approval_id": approval["payload"]["approval_id"],
                "allow": False,
                "allow_for_session": False,
            },
            "mandatory-deny",
        )
    )
    _wait_for(out, lambda line: line.get("type") == "message_end")
    assert _response_for(out, "mandatory-deny")["ok"] is True
    assert decisions == [False]
    bridge.close()


@pytest.mark.parametrize(
    ("host_capabilities", "error_code"),
    [
        ({"protocol_version": "2", "actions": []}, "unsupported_host_action_protocol"),
        (
            {"protocol_version": "1", "actions": ["tasks.list", "tasks.list"]},
            "invalid_host_capabilities",
        ),
        (
            {"protocol_version": "1", "actions": ["workspace.delete"]},
            "unsupported_host_action",
        ),
    ],
)
def test_session_create_rejects_invalid_host_capabilities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_capabilities: dict[str, Any],
    error_code: str,
) -> None:
    out = io.StringIO()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))
    bridge = StdioBridge(stdout=out)

    bridge.process_line(
        _request(
            "session.create",
            {
                "workspace": os.fspath(tmp_path),
                "mode": "auto",
                "workspace_trusted": True,
                "host_capabilities": host_capabilities,
            },
            "create",
        )
    )

    response = _response_for(out, "create")
    assert response["ok"] is False
    assert response["error"]["code"] == error_code
    bridge.close()


def test_host_action_rejects_oversized_then_accepts_valid_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, session_id, _captured, results, _errors = _make_bridge(tmp_path, monkeypatch)
    event = _start_host_action(out, bridge, session_id)
    oversized = {
        "tasks": [
            {"id": f"task-{index}", "label": "x" * 512, "detail": "y" * 512} for index in range(100)
        ],
        "truncated": False,
    }

    _respond(bridge, event, request_id="oversized", result=oversized)
    rejected = _response_for(out, "oversized")
    assert rejected["ok"] is False
    assert rejected["error"]["code"] == "host_action_result_too_large"

    _respond(
        bridge,
        event,
        request_id="valid",
        result={"tasks": [], "truncated": False},
    )
    assert _response_for(out, "valid")["ok"] is True
    _wait_for(out, lambda line: line.get("type") == "message_end")
    assert results == [{"tasks": [], "truncated": False}]
    bridge.close()


def test_host_action_duplicate_response_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, session_id, _captured, _results, _errors = _make_bridge(tmp_path, monkeypatch)
    event = _start_host_action(out, bridge, session_id)
    result = {"tasks": [], "truncated": False}

    _respond(bridge, event, request_id="first", result=result)
    assert _response_for(out, "first")["ok"] is True
    _wait_for(out, lambda line: line.get("type") == "message_end")
    _respond(bridge, event, request_id="duplicate", result=result)

    duplicate = _response_for(out, "duplicate")
    assert duplicate["ok"] is False
    assert duplicate["error"]["code"] == "duplicate_host_action_response"
    bridge.close()


def test_host_action_timeout_emits_cancellation_and_rejects_late_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, session_id, _captured, _results, errors = _make_bridge(
        tmp_path, monkeypatch, timeout=0.08
    )
    event = _start_host_action(out, bridge, session_id)

    cancelled = _wait_for(out, lambda line: line.get("type") == "host_action_cancelled")
    _wait_for(out, lambda line: line.get("type") == "message_end")
    assert cancelled["payload"]["reason"] == "deadline_exceeded"
    assert [error.code for error in errors] == ["host_action_timeout"]
    assert len([line for line in _lines(out) if line.get("type") == "host_action_cancelled"]) == 1

    _respond(
        bridge,
        event,
        request_id="late",
        result={"tasks": [], "truncated": False},
    )
    late = _response_for(out, "late")
    assert late["ok"] is False
    assert late["error"]["code"] == "stale_host_action_response"
    bridge.close()


def test_legacy_mutation_timeout_is_unknown_and_never_retryable(tmp_path, monkeypatch):
    out, bridge, sid, _captured, results, errors = _make_bridge(
        tmp_path,
        monkeypatch,
        actions=("browser.close",),
        action="browser.close",
        arguments={"tab_id": "fixture-tab"},
        timeout=0.08,
        approval_protocol=0,
    )
    try:
        _start_host_action(out, bridge, sid)
        _wait_for(out, lambda line: line.get("type") == "message_end")
        assert not results and len(errors) == 1
        assert errors[0].code == "host_action_timeout"
        assert errors[0].dispatch_state == "unknown"
        assert errors[0].retryable is False
        assert "may have completed" in str(errors[0])
    finally:
        bridge.close()


def test_session_cancellation_deterministically_cancels_host_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, session_id, _captured, _results, errors = _make_bridge(
        tmp_path, monkeypatch, timeout=1.0
    )
    event = _start_host_action(out, bridge, session_id)

    bridge.process_line(
        _request(
            "session.cancel",
            {"session_id": session_id, "reason": "cancelled_by_test"},
            "cancel",
        )
    )
    assert _response_for(out, "cancel")["ok"] is True
    cancelled = _wait_for(out, lambda line: line.get("type") == "host_action_cancelled")
    _wait_for(out, lambda line: line.get("type") == "message_end")

    assert cancelled["payload"]["reason"] == "cancelled_by_test"
    assert [error.code for error in errors] == ["host_action_cancelled"]
    assert len([line for line in _lines(out) if line.get("type") == "host_action_cancelled"]) == 1

    _respond(
        bridge,
        event,
        request_id="late-after-cancel",
        result={"tasks": [], "truncated": False},
    )
    late = _response_for(out, "late-after-cancel")
    assert late["ok"] is False
    assert late["error"]["code"] == "stale_host_action_response"
    bridge.close()


@pytest.mark.parametrize(
    ("action", "arguments", "asks"),
    [
        ("github.git.push", {"branch": "main"}, True),
        ("github.git.fetch", {}, False),
    ],
)
def test_full_access_still_asks_a_person_for_remote_connector_writes(
    tmp_path, monkeypatch, action, arguments, asks
):
    out, bridge, sid, _captured, results, errors = _make_bridge(
        tmp_path,
        monkeypatch,
        actions=(action,),
        action=action,
        arguments=arguments,
        timeout=2.0,
        approval_protocol=1,
        mode="fullaccess",
        explicit_github_all=True,
    )
    try:
        prepared = _start_host_action(out, bridge, sid)
        assert prepared["payload"]["phase"] == "prepare"
        _respond(
            bridge,
            prepared,
            request_id="prepared",
            result={
                "preparation_id": "pa_" + "b" * 32,
                "required": True,
                "write_effect": True,
                "kind": "mcp",
                "reason": "Fixture",
                "preview": f"{action} fixture",
                "target": "@fixture: owner/repo",
            },
        )
        next_line = _wait_for(
            out,
            lambda line: (
                (
                    line.get("type") == "prompt_for_input"
                    and line.get("payload", {}).get("kind") == "approval"
                )
                or (
                    line.get("type") == "host_action_requested"
                    and line["payload"].get("phase") in {"execute", "discard"}
                )
            ),
        )
        if not asks:
            assert next_line["payload"]["phase"] == "execute"
            return
        assert next_line["type"] == "prompt_for_input"
        metadata = next_line["payload"]["metadata"]
        assert metadata["always_ask_person"] is True
        assert metadata["prepared_action"] == "pa_" + "b" * 32
        assert next_line["payload"]["allow_for_session_supported"] is False
        bridge.process_line(
            _request(
                "approval.respond",
                {
                    "session_id": sid,
                    "approval_id": next_line["payload"]["approval_id"],
                    "allow": False,
                    "allow_for_session": False,
                },
                "deny",
            )
        )
        discard = _wait_for(
            out,
            lambda line: (
                line.get("type") == "host_action_requested"
                and line["payload"].get("phase") in {"execute", "discard"}
            ),
        )
        assert discard["payload"]["phase"] == "discard"
        _respond(bridge, discard, request_id="discarded", result={"discarded": True})
        _wait_for(out, lambda line: line.get("type") == "message_end")
        assert not results and len(errors) == 1
        assert errors[0].dispatch_state == "not_dispatched"
    finally:
        bridge.close()


@pytest.mark.parametrize("dispatched", [False, True])
def test_unified_write_timeout_requires_actual_dispatch_evidence(tmp_path, monkeypatch, dispatched):
    out, bridge, sid, _captured, results, errors = _make_bridge(
        tmp_path,
        monkeypatch,
        actions=("browser.close",),
        action="browser.close",
        arguments={"tab_id": "fixture-tab"},
        timeout=0.15,
        approval_protocol=1,
        mode="fullaccess",
    )
    try:
        prepared = _start_host_action(out, bridge, sid)
        assert prepared["payload"]["phase"] == "prepare"
        _respond(
            bridge,
            prepared,
            request_id="prepared",
            result={
                "preparation_id": "pa_" + "a" * 32,
                "required": True,
                "write_effect": True,
                "kind": "application-control",
                "reason": "Fixture",
                "preview": "Close fixture tab",
                "target": "fixture-tab",
            },
        )
        execute = _wait_for(
            out,
            lambda line: (
                line.get("type") == "host_action_requested"
                and line["payload"].get("phase") == "execute"
            ),
        )
        payload = execute["payload"]
        marker = {
            "session_id": sid,
            "host_action_id": payload["host_action_id"],
            "workspace_fence": payload["workspace_fence"],
            "capability_fingerprint": payload["capability_fingerprint"],
        }
        if dispatched:
            bridge.process_line(_request("host.action.dispatched", marker, "marker"))
            assert _response_for(out, "marker")["ok"]
            bridge.process_line(_request("host.action.dispatched", marker, "duplicate-marker"))
            assert (
                _response_for(out, "duplicate-marker")["error"]["code"] == "duplicate_host_dispatch"
            )
        _wait_for(out, lambda line: line.get("type") == "host_action_cancelled")
        _wait_for(out, lambda line: line.get("type") == "message_end")
        assert not results and len(errors) == 1
        assert errors[0].code == "host_action_timeout"
        assert errors[0].dispatch_state == ("dispatched" if dispatched else "not_dispatched")
        assert errors[0].retryable is (not dispatched)
        assert ("may have completed" in str(errors[0])) is dispatched
        bridge.process_line(_request("host.action.dispatched", marker, "late-marker"))
        assert _response_for(out, "late-marker")["error"]["code"] == "stale_host_action"
    finally:
        bridge.close()
