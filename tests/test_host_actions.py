from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent.errors import AgentRuntimeError
from alysis_code.agent.tools_assembly import build_tools
from alysis_code.config import AppConfig
from alysis_code.host_actions import (
    HOST_ACTION_TOOL_NAMES,
    HOST_ACTIONS,
    HostActionError,
    normalize_host_action_arguments,
    normalize_host_action_result,
    normalize_host_error,
    normalized_host_action_capabilities,
)
from alysis_code.session_store import SessionStore
from alysis_code.surface import ApprovalDecision, ApprovalRequest, NoopSurface
from alysis_code.tools.registry import get_builtin_tool_metadata


def _store(root: Path) -> SessionStore:
    return SessionStore(
        enabled=False,
        sessions_dir=root / ".sessions",
        session_id="host-actions-test",
        cwd=str(root),
        repo_root=str(root),
    )


def _build_tools(
    root: Path,
    *,
    mode: str = "auto",
    actions: tuple[str, ...] = (),
    handler: Any | None = None,
    surface: Any | None = None,
    host_mcp: dict[str, Any] | None = None,
    google_mcp: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return build_tools(
        root=root,
        console=None,
        store=_store(root),
        mode=mode,
        yes=True,
        cfg=AppConfig(model="test-model"),
        surface=surface,
        host_action_capabilities=actions,
        host_action_handler=handler,
        host_mcp=host_mcp,
        google_mcp=google_mcp,
    )


class _ApprovalSurface(NoopSurface):
    host_managed_approvals = True

    def __init__(self, decision: ApprovalDecision) -> None:
        self.decision = decision
        self.requests: list[ApprovalRequest] = []

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return self.decision


def test_host_action_tool_metadata_is_complete() -> None:
    # MCP tool schemas come from the negotiated catalog rather than built-in metadata.
    assert set(HOST_ACTION_TOOL_NAMES) == set(HOST_ACTIONS) - {
        "mcp.github.call",
        "mcp.github.discover",
        "mcp.github.describe",
        "mcp.google.call",
    }
    for action in HOST_ACTION_TOOL_NAMES:
        tool_name = HOST_ACTION_TOOL_NAMES[action]
        metadata = get_builtin_tool_metadata(tool_name)
        assert metadata is not None
        assert metadata.optional is True
        assert "ide" in metadata.categories


def test_host_action_tools_are_absent_without_negotiated_capabilities(tmp_path: Path) -> None:
    tools = _build_tools(tmp_path, handler=lambda _action, _arguments: {})

    assert not {name for name in tools if name.startswith("ide_task_")}
    assert not {name for name in tools if name.startswith("ide_debug_")}


def test_host_action_tools_are_exposed_per_capability_and_dispatch_bounded_args(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def handler(action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((action, arguments))
        return {"tasks": [], "truncated": False}

    tools = _build_tools(
        tmp_path,
        actions=("tasks.list",),
        handler=handler,
    )

    assert "ide_task_list" in tools
    assert "ide_task_run" not in tools
    assert tools["ide_task_list"].run({"task_type": "npm"}) == {
        "tasks": [],
        "truncated": False,
    }
    assert calls == [("tasks.list", {"task_type": "npm"})]


def test_readonly_host_tools_only_expose_read_operations(tmp_path: Path) -> None:
    tools = _build_tools(
        tmp_path,
        mode="readonly",
        actions=HOST_ACTIONS,
        handler=lambda _action, _arguments: {},
    )

    assert {"ide_task_list", "ide_task_status", "ide_debug_list", "ide_debug_status"} <= set(tools)
    assert "ide_task_run" not in tools
    assert "ide_task_terminate" not in tools
    assert "ide_debug_start" not in tools
    assert "ide_debug_stop" not in tools


def test_host_action_errors_reach_model_as_structured_runtime_errors(tmp_path: Path) -> None:
    def handler(_action: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        raise HostActionError("host_action_timeout", "Host action timed out.", retryable=True)

    tools = _build_tools(tmp_path, actions=("tasks.list",), handler=handler)

    with pytest.raises(AgentRuntimeError) as exc_info:
        tools["ide_task_list"].run({})

    assert exc_info.value.result_payload == {
        "error": "Host action timed out.",
        "error_code": "host_action_timeout",
        "retryable": True,
    }


def test_task_run_result_normalizes_bounded_diagnostics_delta() -> None:
    result = normalize_host_action_result(
        "tasks.run",
        {
            "execution_id": "execution-1",
            "task_id": "task-1",
            "state": "completed",
            "exit_code": 0,
            "diagnostics_delta": {
                "added": 1,
                "removed": 0,
                "changed": 0,
                "total": 1,
                "truncated": False,
                "items": [
                    {
                        "uri": "file:///workspace/src/app.py",
                        "line": 7,
                        "severity": "error",
                        "message": "Example error",
                        "source": "pytest",
                    }
                ],
            },
        },
    )

    assert result["state"] == "completed"
    assert result["diagnostics_delta"]["items"][0]["line"] == 7


def test_host_action_result_rejects_unbounded_payload() -> None:
    with pytest.raises(HostActionError) as exc_info:
        normalize_host_action_result(
            "tasks.list",
            {
                "tasks": [
                    {
                        "id": f"task-{index}",
                        "label": "x" * 512,
                        "detail": "y" * 512,
                    }
                    for index in range(100)
                ],
                "truncated": False,
            },
        )

    assert exc_info.value.code == "host_action_result_too_large"


def test_host_action_result_rejects_non_boolean_truncation_marker() -> None:
    with pytest.raises(HostActionError) as exc_info:
        normalize_host_action_result(
            "tasks.list",
            {"tasks": [], "truncated": "false"},
        )

    assert exc_info.value.code == "invalid_host_action_payload"


def test_capability_normalization_intersects_the_supported_allowlist() -> None:
    assert normalized_host_action_capabilities(
        ["tasks.list", " tasks.status ", "unknown.action", 7]  # type: ignore[list-item]
    ) == frozenset({"tasks.list", "tasks.status"})


@pytest.mark.parametrize("value", [{"x": 1}, ["x"], 7, True])
@pytest.mark.parametrize("field", ["identifier", "label", "enum"])
def test_host_action_result_rejects_non_string_string_fields(field: str, value: Any) -> None:
    if field == "identifier":
        action = "tasks.list"
        result = {"tasks": [{"id": value, "label": "Task"}], "truncated": False}
    elif field == "label":
        action = "tasks.list"
        result = {"tasks": [{"id": "task-1", "label": value}], "truncated": False}
    else:
        action = "tasks.run"
        result = {
            "execution_id": "execution-1",
            "task_id": "task-1",
            "state": value,
        }

    with pytest.raises(HostActionError) as exc_info:
        normalize_host_action_result(action, result)

    assert exc_info.value.code == "invalid_host_action_payload"


@pytest.mark.parametrize("field", ["code", "message"])
@pytest.mark.parametrize("value", [{"x": 1}, ["x"], 7, True])
def test_host_error_rejects_non_string_code_and_message(field: str, value: Any) -> None:
    error: dict[str, Any] = {
        "code": "task_failed",
        "message": "Task failed.",
        "retryable": False,
    }
    error[field] = value

    with pytest.raises(HostActionError) as exc_info:
        normalize_host_error(error)

    assert exc_info.value.code == "invalid_host_action_error"


@pytest.mark.parametrize("control", ["\x00", "\x08", "\x0b", "\x0c", "\x1f", "\x7f"])
def test_host_action_labels_reject_disallowed_controls(control: str) -> None:
    with pytest.raises(HostActionError) as exc_info:
        normalize_host_action_result(
            "tasks.list",
            {"tasks": [{"id": "task-1", "label": f"bad{control}label"}], "truncated": False},
        )

    assert exc_info.value.code == "invalid_host_action_payload"


def test_task_status_supports_optional_filter_and_bounded_terminal_records() -> None:
    assert normalize_host_action_arguments("tasks.status", {}) == {}
    assert normalize_host_action_arguments("tasks.status", {"execution_id": "exec-1"}) == {
        "execution_id": "exec-1"
    }
    assert normalize_host_action_result(
        "tasks.status",
        {
            "executions": [
                {
                    "execution_id": "exec-1",
                    "task_id": "task-1",
                    "state": "completed",
                    "exit_code": 0,
                    "diagnostics_delta": {
                        "added": 0,
                        "removed": 0,
                        "changed": 0,
                        "total": 0,
                        "truncated": False,
                        "items": [],
                    },
                }
            ],
            "truncated": False,
        },
    ) == {
        "executions": [
            {
                "execution_id": "exec-1",
                "task_id": "task-1",
                "state": "completed",
                "exit_code": 0,
                "diagnostics_delta": {
                    "added": 0,
                    "removed": 0,
                    "changed": 0,
                    "total": 0,
                    "truncated": False,
                    "items": [],
                },
            }
        ],
        "truncated": False,
    }


@pytest.mark.parametrize(
    ("tool_name", "action", "arguments"),
    [
        ("ide_task_run", "tasks.run", {"task_id": "task-1"}),
        ("ide_debug_start", "debug.start", {"configuration_id": "debug-1"}),
    ],
)
def test_opaque_host_execution_cannot_run_silently_in_auto_mode(
    tmp_path: Path,
    tool_name: str,
    action: str,
    arguments: dict[str, str],
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    tools = _build_tools(
        tmp_path,
        actions=(action,),
        handler=lambda host_action, args: calls.append((host_action, args)) or {},
    )

    with pytest.raises(AgentRuntimeError, match="one-time IDE approval"):
        tools[tool_name].run(arguments)

    assert calls == []


def test_denied_opaque_host_execution_emits_no_host_request(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    surface = _ApprovalSurface(ApprovalDecision(allow=False))
    tools = _build_tools(
        tmp_path,
        actions=("tasks.run",),
        handler=lambda action, args: calls.append((action, args)) or {},
        surface=surface,
    )

    with pytest.raises(AgentRuntimeError, match="User declined"):
        tools["ide_task_run"].run({"task_id": "task-1"})

    assert calls == []
    assert len(surface.requests) == 1
    request = surface.requests[0]
    assert request.allow_for_session_scope is None
    assert request.metadata["mandatory_explicit_approval"] is True
    assert request.metadata["allow_for_session_disabled"] is True


def test_session_style_approval_cannot_authorize_opaque_host_execution(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    surface = _ApprovalSurface(ApprovalDecision(allow=True, allow_for_session=True))
    tools = _build_tools(
        tmp_path,
        actions=("debug.start",),
        handler=lambda action, args: calls.append((action, args)) or {},
        surface=surface,
    )

    with pytest.raises(AgentRuntimeError, match="Session approval cannot authorize"):
        tools["ide_debug_start"].run({"configuration_id": "debug-1"})

    assert calls == []


def test_fullaccess_cannot_bypass_opaque_host_execution_approval(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    tools = _build_tools(
        tmp_path,
        mode="fullaccess",
        actions=("tasks.run",),
        handler=lambda action, args: calls.append((action, args)) or {},
    )

    with pytest.raises(AgentRuntimeError, match="one-time IDE approval"):
        tools["ide_task_run"].run({"task_id": "task-1"})

    assert calls == []


@pytest.mark.parametrize(
    ("tool_name", "action", "arguments"),
    [
        ("ide_task_run", "tasks.run", {"task_id": "task-1"}),
        ("ide_debug_start", "debug.start", {"configuration_id": "debug-1"}),
    ],
)
@pytest.mark.parametrize(
    "decision",
    [
        ApprovalDecision(allow=True),
        ApprovalDecision(allow=True, allow_for_session=True),
        ApprovalDecision(allow=False),
    ],
)
def test_fullaccess_dispatches_opaque_host_execution_without_prompt(
    tmp_path: Path,
    tool_name: str,
    action: str,
    arguments: dict[str, str],
    decision: ApprovalDecision,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    # Full access authorizes this request by mode, so the surface's hypothetical
    # answer must neither be requested nor affect dispatch.
    surface = _ApprovalSurface(decision)
    tools = _build_tools(
        tmp_path,
        mode="fullaccess",
        actions=(action,),
        handler=lambda action, args: calls.append((action, args)) or {},
        surface=surface,
    )

    assert tools[tool_name].run(arguments) == {}
    assert calls == [(action, arguments)]
    assert surface.requests == []


def test_allow_once_dispatches_exact_opaque_host_execution(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    surface = _ApprovalSurface(ApprovalDecision(allow=True, allow_for_session=False))
    tools = _build_tools(
        tmp_path,
        actions=("tasks.run",),
        handler=lambda action, args: (
            calls.append((action, args))
            or {
                "execution_id": "exec-1",
                "task_id": "task-1",
                "state": "started",
            }
        ),
        surface=surface,
    )

    result = tools["ide_task_run"].run({"task_id": "task-1"})

    assert result["execution_id"] == "exec-1"
    assert calls == [("tasks.run", {"task_id": "task-1"})]
    assert "task_id: task-1" in surface.requests[0].preview
    assert str(tmp_path.resolve()) in surface.requests[0].preview


@pytest.mark.parametrize("mode", ["readonly", "auto", "fullaccess"])
def test_desktop_terminal_tools_require_capability_and_full_access(
    tmp_path: Path, mode: str
) -> None:
    calls = []
    tools = _build_tools(
        tmp_path,
        mode=mode,
        actions=HOST_ACTIONS,
        handler=lambda action, args: calls.append((action, args)) or {"accepted": True},
    )
    assert "desktop_terminal_list" in tools
    assert "desktop_terminal_read" in tools
    for name in ("open", "write", "close"):
        assert (f"desktop_terminal_{name}" in tools) == (mode == "fullaccess")
    if mode == "fullaccess":
        tools["desktop_terminal_write"].run({"terminal_id": "one", "text": "Καλημέρα 世界\n"})
        assert calls == [("terminal.write", {"terminal_id": "one", "text": "Καλημέρα 世界\n"})]
    unbound = _build_tools(tmp_path, mode="fullaccess", handler=lambda *_: {})
    assert not any(name.startswith("desktop_terminal_") for name in unbound)


@pytest.mark.parametrize(
    "action,args",
    [
        ("terminal.open", {"cwd": "/other"}),
        ("terminal.read", {"terminal_id": "one", "since": True}),
        ("terminal.read", {"terminal_id": "one", "since": -1}),
        ("terminal.write", {"terminal_id": "one", "text": "x" * 4097}),
        ("terminal.close", {"terminal_id": "one\n"}),
    ],
)
def test_desktop_terminal_arguments_are_bounded(action: str, args: dict[str, Any]) -> None:
    with pytest.raises(HostActionError):
        normalize_host_action_arguments(action, args)


def test_desktop_terminal_output_is_bounded_and_unicode_preserved() -> None:
    result = {
        "terminal_id": "one",
        "text": "Καλημέρα 世界",
        "next_seq": 2,
        "truncated": False,
        "status": "running",
        "exit_code": None,
    }
    assert normalize_host_action_result("terminal.read", result) == result
    with pytest.raises(HostActionError):
        normalize_host_action_result("terminal.read", {**result, "text": "x" * 12001})


def test_browser_writes_are_available_without_routine_approval_but_not_in_plan(
    tmp_path: Path,
) -> None:
    surface = _ApprovalSurface(ApprovalDecision(allow=False))
    calls = []
    tools = _build_tools(
        tmp_path,
        mode="auto",
        actions=("browser.snapshot", "browser.click"),
        handler=lambda action, args: calls.append((action, args)) or {"ok": True},
        surface=surface,
    )
    assert tools["embedded_browser_click"].run(
        {"tab_id": "tab", "snapshot_id": "s", "ref": "e"}
    ) == {"ok": True}
    assert not surface.requests
    assert calls[0][0] == "browser.click"
    readonly = _build_tools(
        tmp_path,
        mode="readonly",
        actions=("browser.snapshot", "browser.click"),
        handler=lambda *_: {},
    )
    assert "embedded_browser_snapshot" in readonly
    assert "embedded_browser_click" not in readonly
    assert "write" in get_builtin_tool_metadata("embedded_browser_click").categories
    assert "read" not in get_builtin_tool_metadata("embedded_browser_click").categories
