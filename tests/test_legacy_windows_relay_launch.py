"""LR-41 legacy availability only: inert Popen ports, never native admission."""

import json
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from alysis_code.config_errors import ConfigError
from alysis_code.mcp import native_relay_guard as guard
from alysis_code.mcp import transport_stdio as transport
from alysis_code.mcp.manager import McpManager
from alysis_code.mcp.models import ResolvedMcpConfig, ResolvedMcpServer
from alysis_code.mcp.server_requests import McpServerRequestContext
from alysis_code.runtime_kind import RuntimeKind


@pytest.fixture
def selected(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    helper = root / "computer-use" / "alysis-computer-use.exe"
    helper.parent.mkdir()
    helper.write_bytes(b"inert selected helper, never executed")
    helper.chmod(0o700)
    control = {
        "ALYSIS_CU_CONTROL_ADDRESS": "127.0.0.1:12345",
        "ALYSIS_CU_CONTROL_TOKEN": "a" * 64,
        "ALYSIS_CU_CONTROL_EPOCH": "b" * 32,
    }
    host = {
        **control,
        "ALYSIS_DESKTOP_TOOL_SERVER_APPROVAL": json.dumps(
            {
                "version": 1,
                "read_marks": str(root / "marks.json"),
                "exempt": [{"server_id": "alysis-computer", "command": str(helper)}],
            }
        ),
    }
    for key, value in host.items():
        monkeypatch.setenv(key, value)
    # Only this module's platform view is simulated, not Python or host OS.
    monkeypatch.setattr(guard, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(transport, "_IS_WINDOWS", True)
    server = ResolvedMcpServer(
        id="alysis-computer",
        transport="stdio",
        enabled=True,
        enabled_in=(RuntimeKind.INTERACTIVE_CHAT,),
        trust="explicit",
        allowed_tools=(),
        denied_tools=(),
        startup_timeout_s=10,
        call_timeout_s=1080,
        tool_prefix=None,
        command=str(helper),
        args=("--allow-all",),
        env=dict(control),
    )
    calls = []
    sentinel = object()
    monkeypatch.setattr(
        transport.subprocess, "Popen", lambda *a, **k: calls.append((a, k)) or sentinel
    )
    return root, helper, server, host, calls, sentinel


def manager(root, server, runtime=RuntimeKind.INTERACTIVE_CHAT):
    config = ResolvedMcpConfig(
        root, root / "user.json", root / "project.json", True, False, (server,)
    )
    return McpManager(resolved_config=config, workspace_root=root, runtime_kind=runtime)


def channel(root, server, host, runtime=RuntimeKind.INTERACTIVE_CHAT):
    context = McpServerRequestContext(server.id, root, runtime) if runtime else None
    return transport.McpStdioTransport(
        server=server, workspace_root=root, host_env=host, server_request_context=context
    )


def test_actual_manager_client_transport_exact_legacy_tuple(selected):
    root, helper, server, host, calls, sentinel = selected
    owner = manager(root, server)
    client = owner._build_live_client(server)
    try:
        assert client.transport._spawn([str(helper), "--allow-all"]) is sentinel
        assert len(calls) == 1
        assert calls[0][0][0] == [str(helper), "--allow-all"]
        assert all(calls[0][1]["env"][key] == value for key, value in server.env.items())
        evidence = client.transport._native_relay_inspection
        assert evidence.state == "unproven"
        assert evidence.obligation == "OBLIGATION-HELPER-ADMISSION"
        assert "native_control_required" in evidence.limitations
    finally:
        client.close()
        owner.close()


@pytest.mark.parametrize("runtime", [r for r in RuntimeKind if r != RuntimeKind.INTERACTIVE_CHAT])
def test_other_runtime_manager_and_transport_refuse(selected, runtime):
    root, helper, server, host, calls, _ = selected
    owner = manager(root, server, runtime)
    try:
        with pytest.raises(ConfigError, match="native Computer Use"):
            owner._build_live_client(server)
        with pytest.raises(ValueError, match="native Computer Use"):
            channel(root, server, host, runtime)._spawn([str(helper), "--allow-all"])
        assert calls == []
    finally:
        owner.close()


@pytest.mark.parametrize(
    "change",
    [
        {"id": "plugin/alysis-computer"},
        {"id": "custom"},
        {"enabled": False},
        {"enabled_in": None},
        {"enabled_in": (RuntimeKind.INTERACTIVE_CHAT, RuntimeKind.SUBAGENT)},
        {"args": ()},
        {"args": ("--allow-all", "--extra")},
        {"trust": "implicit"},
        {"tool_prefix": "native"},
        {"roots_mode": "workspace"},
        {"prompts_mode": "listed_get_only"},
        {"resources_mode": "listed_read_only"},
        {"allowed_tools": ("quit_application",)},
        {"call_timeout_s": 1081},
        {"startup_timeout_s": float("nan")},
        {"env": {}},
        {"url": "http://localhost/"},
    ],
)
def test_nonselected_registration_refuses_before_effect(selected, change):
    root, helper, server, host, calls, _ = selected
    server = replace(server, **change)
    assert guard.legacy_windows_launch(server, RuntimeKind.INTERACTIVE_CHAT, host) is None
    with pytest.raises(ValueError, match="native Computer Use"):
        channel(root, server, host)._spawn([str(helper), *server.args])
    assert calls == []


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "wrong-path",
        "duplicate",
        "bool-version",
        "unresolved-token",
        "overlay",
        "remote-address",
        "wrong-epoch",
    ],
)
def test_selection_and_control_inputs_are_required(selected, kind):
    root, helper, server, host, calls, _ = selected
    if kind == "missing":
        host.pop("ALYSIS_DESKTOP_TOOL_SERVER_APPROVAL")
    elif kind == "wrong-path":
        host["ALYSIS_DESKTOP_TOOL_SERVER_APPROVAL"] = json.dumps(
            {
                "version": 1,
                "exempt": [{"server_id": "alysis-computer", "command": str(helper) + "x"}],
            }
        )
    elif kind == "duplicate":
        host["ALYSIS_DESKTOP_TOOL_SERVER_APPROVAL"] = '{"version":1,"version":1,"exempt":[]}'
    elif kind == "bool-version":
        host["ALYSIS_DESKTOP_TOOL_SERVER_APPROVAL"] = host[
            "ALYSIS_DESKTOP_TOOL_SERVER_APPROVAL"
        ].replace('"version": 1', '"version": true')
    elif kind == "unresolved-token":
        server.env["ALYSIS_CU_CONTROL_TOKEN"] = "${ALYSIS_CU_CONTROL_TOKEN}"
    elif kind == "overlay":
        server.env["PYTHONPATH"] = str(root)
    elif kind == "remote-address":
        host["ALYSIS_CU_CONTROL_ADDRESS"] = server.env["ALYSIS_CU_CONTROL_ADDRESS"] = (
            "0.0.0.0:12345"
        )
    elif kind == "wrong-epoch":
        host["ALYSIS_CU_CONTROL_EPOCH"] = "c" * 32
    with pytest.raises(ValueError, match="native Computer Use"):
        channel(root, server, host)._spawn([str(helper), "--allow-all"])
    assert calls == []


@pytest.mark.parametrize(
    "mutation", ["epoch", "token", "file", "spawn-env", "argv", "alias", "registration"]
)
def test_captured_tuple_rechecked_at_final_spawn(selected, mutation):
    root, helper, server, host, calls, _ = selected
    endpoint = channel(root, server, host)
    assert endpoint._legacy_windows_launch is not None
    launch = [str(helper), "--allow-all"]
    if mutation in {"epoch", "token"}:
        key = "ALYSIS_CU_CONTROL_" + mutation.upper()
        host[key] = server.env[key] = "d" * (32 if mutation == "epoch" else 64)
    elif mutation == "file":
        helper.unlink()
        helper.write_bytes(b"replacement")
    elif mutation == "spawn-env":
        endpoint.spawn_env["ALYSIS_CU_CONTROL_TOKEN"] = "e" * 64
    elif mutation == "argv":
        launch.append("--new")
    elif mutation == "alias":
        launch[0] = str(helper.parent) + "/../computer-use/" + helper.name
    elif mutation == "registration":
        endpoint.server = replace(server, args=())
    with pytest.raises(ValueError, match="native Computer Use"):
        endpoint._spawn(launch)
    assert calls == []


@pytest.mark.parametrize(
    "kind", ["symlink", "hardlink", "relative", "shell", "no-context", "darwin", "forged-env"]
)
def test_no_alias_fallback_or_mac_socket_exception(selected, monkeypatch, kind, request):
    root, helper, server, host, calls, _ = selected
    context = RuntimeKind.INTERACTIVE_CHAT
    if kind == "symlink":
        request.getfixturevalue("require_symlinks")
        original = helper.with_name("selected-original")
        helper.rename(original)
        helper.symlink_to(original)
    elif kind == "hardlink":
        os.link(helper, helper.with_name("alias"))
    elif kind == "relative":
        server = replace(server, command="alysis-computer-use.exe")
    elif kind == "shell":
        server = replace(server, command="/bin/sh", args=("-c", str(helper)))
    elif kind == "no-context":
        context = None
    elif kind in {"darwin", "forged-env"}:
        monkeypatch.setattr(guard, "sys", SimpleNamespace(platform="darwin"))
        if kind == "forged-env":
            host["ALYSIS_PLATFORM"] = "win32"
    endpoint = channel(root, server, host, context)
    assert endpoint._legacy_windows_launch is None
    with pytest.raises(
        (ValueError, FileNotFoundError), match="native Computer Use|could not be resolved"
    ):
        endpoint._spawn([server.command, *server.args])
    assert calls == []


def test_exact_tuple_cannot_use_windows_fallback(selected):
    root, helper, server, host, calls, _ = selected
    endpoint = channel(root, server, host)
    with pytest.raises(transport.McpStdioTransportError):
        endpoint._spawn_resolved_windows_command(FileNotFoundError("original failed"))
    assert calls == []


def test_catalog_preserves_user_restrictions_and_excludes_new_tools(selected):
    root, helper, server, host, calls, _ = selected
    server = replace(server, denied_tools=("computer_wait",), call_timeout_s=30)
    assert guard.legacy_windows_launch(server, RuntimeKind.INTERACTIVE_CHAT, host)
    owner = manager(root, server)
    tools = tuple(
        SimpleNamespace(name=name)
        for name in (
            "computer_state",
            "computer_action",
            "computer_wait",
            "focused_input",
            "quit_application",
            "Computer_State",
        )
    )
    # Duplicate case is still rejected before filtering.
    with pytest.raises(ConfigError, match="duplicate"):
        owner._filter_server_tools(server=server, raw_tools=tools)
    filtered, names = owner._filter_server_tools(server=server, raw_tools=tools[:-1])
    assert [tool.name for tool in filtered] == ["computer_state", "computer_action"]
    ordinary = replace(server, id="ordinary-plugin")
    filtered, _ = owner._filter_server_tools(server=ordinary, raw_tools=tools[:-1])
    assert [tool.name for tool in filtered] == [
        "computer_state",
        "computer_action",
        "focused_input",
        "quit_application",
    ]
    assert calls == []
    owner.close()


def test_actual_config_token_reference_to_original_client_transport(selected, monkeypatch):
    from alysis_code.mcp.config import load_resolved_mcp_config, user_mcp_config_path

    root, helper, original, host, calls, sentinel = selected
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(root / "config"))
    path = user_mcp_config_path()
    path.parent.mkdir()
    payload = dict(
        transport="stdio",
        command=str(helper),
        args=["--allow-all"],
        env={**original.env, "ALYSIS_CU_CONTROL_TOKEN": "${ALYSIS_CU_CONTROL_TOKEN}"},
        enabled=True,
        enabled_in=["interactive_chat"],
        trust="explicit",
        allowed_tools=["computer_state"],
        denied_tools=[],
        startup_timeout_s=10,
        call_timeout_s=1080,
        tool_prefix=None,
        roots_mode="disabled",
        resources_mode="disabled",
        prompts_mode="disabled",
    )
    path.write_text(json.dumps({"servers": {"alysis-computer": payload}}))
    config = load_resolved_mcp_config(workspace_root=root)
    (server,) = config.servers
    assert server.env == original.env
    owner = McpManager(resolved_config=config, workspace_root=root, runtime_kind="interactive_chat")
    client = owner._build_live_client(server)
    try:
        assert client.transport._spawn([str(helper), "--allow-all"]) is sentinel
        assert len(calls) == 1
    finally:
        client.close()
        owner.close()


@pytest.mark.parametrize("change", ["server", "workspace"])
def test_cross_bound_context_cannot_select_tuple(selected, change):
    root, helper, server, host, calls, _ = selected
    context = McpServerRequestContext(
        "other" if change == "server" else server.id,
        root / "other" if change == "workspace" else root,
        RuntimeKind.INTERACTIVE_CHAT,
    )
    endpoint = transport.McpStdioTransport(
        server=server, workspace_root=root, host_env=host, server_request_context=context
    )
    with pytest.raises(ValueError, match="native Computer Use"):
        endpoint._spawn([str(helper), "--allow-all"])
    assert calls == []


def test_mac_old_selected_socket_tuple_stays_denied(selected, monkeypatch):
    root, _, server, host, calls, _ = selected
    monkeypatch.setattr(guard, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(transport, "_IS_WINDOWS", False)
    relay = root / "alysis-computer-mcp"
    relay.write_bytes(b"old socket relay fixture")
    relay.chmod(0o700)
    server = replace(server, command=str(relay), args=(str(root / "computer.sock"),), env={})
    host["ALYSIS_DESKTOP_TOOL_SERVER_APPROVAL"] = json.dumps(
        {"version": 1, "exempt": [{"server_id": "alysis-computer", "command": str(relay)}]}
    )
    monkeypatch.setenv(
        "ALYSIS_DESKTOP_TOOL_SERVER_APPROVAL", host["ALYSIS_DESKTOP_TOOL_SERVER_APPROVAL"]
    )
    owner = manager(root, server)
    with pytest.raises(ConfigError, match="native Computer Use"):
        owner._build_live_client(server)
    with pytest.raises(ValueError, match="native Computer Use"):
        channel(root, server, host)._spawn([str(relay), *server.args])
    assert calls == []
    owner.close()


def refusal_payload(response):
    """The structured tool error the model reads; nothing was dispatched."""
    assert response.error is None and response.result["isError"] is True
    [item] = response.result["content"]
    assert item["type"] == "text"
    error = json.loads(item["text"])["error"]
    assert set(error) == {"code", "message", "dispatch"} and error["dispatch"] == "not_dispatched"
    return error


def refused(selected, monkeypatch, name, args):
    root, _, server, host, calls, _ = selected
    endpoint = channel(root, server, host)
    starts, writes = [], []
    monkeypatch.setattr(endpoint, "start", lambda: starts.append(True))
    monkeypatch.setattr(endpoint, "_send_serialized", lambda value: writes.append(value))
    response = endpoint.request(
        method="tools/call", params={"name": name, "arguments": args}, timeout_s=1
    )
    assert starts == writes == calls == []
    assert endpoint._pending == {}
    return refusal_payload(response)


def drag(**fields):
    return {"action": "drag", "snapshot_id": "s", **fields}


@pytest.mark.parametrize(
    "name,args,code",
    [
        ("quit_application", {}, "unsupported_action"),
        ("computer_action", {"action": "focused_input"}, "unsupported_action"),
        ("computer_action", {"action": "history_back"}, "unsupported_action"),
        ("computer_action", {"action": "quit_application"}, "unsupported_action"),
        (
            "computer_action",
            {"action": "type", "element_id": "focus", "text": "x"},
            "unsupported_action",
        ),
        ("computer_action", drag(element_id=" focus ", to_x=1, to_y=2), "unsupported_action"),
        ("computer_action", {"action": "click", "target_type": "visual_point"}, "invalid_action"),
        ("computer_action", {"action": "press", "window_ref": None}, "invalid_action"),
        (
            "computer_action",
            {"action": "click", "element_id": "e1", "to_x": 1, "to_y": 2},
            "invalid_action",
        ),
        ("computer_action", {"action": "key", "keys": ["a"] * 9}, "invalid_keys"),
        ("computer_action", {"action": "key", "keys": []}, "invalid_keys"),
        ("computer_action", {"action": "key", "keys": "a"}, "invalid_keys"),
        ("computer_action", {"action": "key", "keys": [1]}, "invalid_keys"),
        ("computer_action", {"action": "key", "keys": ["shift"]}, "invalid_keys"),
        ("computer_action", {"action": "key", "keys": ["f4"]}, "invalid_keys"),
        ("computer_action", {"action": "key", "keys": ["pageup"]}, "invalid_keys"),
        ("computer_action", {"action": "key", "keys": ["\u20ac"]}, "invalid_keys"),
        ("computer_action", {"action": "key", "keys": ["ab"]}, "invalid_keys"),
        ("computer_action", {"action": "key", "keys": [" "]}, "invalid_keys"),
        ("computer_action", {"action": "key", "keys": ["shift", "a", "b"]}, "invalid_keys"),
        ("computer_action", {"action": "key", "keys": ["home", "shift", "end"]}, "invalid_keys"),
        ("computer_action", {"action": "key", "keys": ["ctrl"]}, "invalid_keys"),
        ("computer_state", {"list_windows": True}, "invalid_action"),
        ("computer_wait", {"duration_ms": 50, "surface_ref": "new"}, "invalid_action"),
    ],
)
def test_new_helper_routes_refuse_before_start_or_write(selected, monkeypatch, name, args, code):
    assert refused(selected, monkeypatch, name, args)["code"] == code


@pytest.mark.parametrize(
    "args,code",
    [
        (drag(element_id="e1"), "invalid_action"),
        (drag(element_id="e1", to_x=1), "invalid_action"),
        (drag(to_x=1, to_y=2), "invalid_action"),
        (drag(x=1, to_x=1, to_y=2), "invalid_action"),
        (drag(element_id="e1", x=1, y=2, to_x=1, to_y=2), "invalid_action"),
        (drag(element_id="e1", to_x="1", to_y=2), "invalid_request"),
        (drag(element_id="e1", to_x=True, to_y=2), "invalid_request"),
        (drag(element_id="e1", to_x=1, to_y=None), "invalid_request"),
        (drag(x=1, y=100001, to_x=1, to_y=2), "invalid_request"),
        (drag(element_id="e1", to_x=1, to_y=2, duration_ms=5001), "invalid_request"),
        (drag(element_id="e1", to_x=1, to_y=2, duration_ms=-1), "invalid_request"),
        (drag(element_id="e1", to_x=1, to_y=2, duration_ms=1.5), "invalid_request"),
        (drag(element_id="e1", to_x=1, to_y=2, text="x"), "invalid_action"),
        (drag(element_id="e1", to_x=1, to_y=2, delta_y=5), "invalid_action"),
        (drag(element_id="e1", to_x=1, to_y=2, input_mode="raw"), "invalid_action"),
    ],
)
def test_malformed_drag_is_a_structured_tool_error(selected, monkeypatch, args, code):
    assert refused(selected, monkeypatch, "computer_action", args)["code"] == code


def test_nonfinite_parameters_are_a_structured_tool_error(selected, monkeypatch):
    error = refused(
        selected, monkeypatch, "computer_action", drag(x=float("nan"), y=1, to_x=1, to_y=2)
    )
    assert error["code"] == "invalid_request"


@pytest.mark.parametrize("field", ["x", "y", "to_x", "to_y"])
@pytest.mark.parametrize(
    "value",
    [10**400, -(10**400), 100_001, -100_001],
    ids=["+10**400", "-10**400", "+100001", "-100001"],
)
def test_oversized_integer_coordinates_are_a_structured_tool_error(
    selected, monkeypatch, field, value
):
    # A JSON integer too large for a float must not escape as OverflowError.
    args = drag(x=1, y=2, to_x=3, to_y=4)
    args[field] = value
    error = refused(selected, monkeypatch, "computer_action", args)
    assert error == {
        "code": "invalid_request",
        "message": f"{field} is invalid.",
        "dispatch": "not_dispatched",
    }


@pytest.mark.parametrize("value", [10**400, -(10**400)], ids=["+10**400", "-10**400"])
def test_oversized_integer_duration_is_a_structured_tool_error(selected, monkeypatch, value):
    error = refused(
        selected,
        monkeypatch,
        "computer_action",
        drag(element_id="e1", to_x=1, to_y=2, duration_ms=value),
    )
    assert error["code"] == "invalid_request"


@pytest.mark.parametrize("value", [True, False])
def test_boolean_coordinates_stay_refused(selected, monkeypatch, value):
    error = refused(selected, monkeypatch, "computer_action", drag(x=value, y=2, to_x=3, to_y=4))
    assert error["code"] == "invalid_request"


def test_oversized_integer_reaches_the_model_as_an_is_error_tool_result(selected, monkeypatch):
    root, _, server, _, calls, _ = selected
    owner = manager(root, server)
    client = owner._build_live_client(server)
    monkeypatch.setattr(client, "_require_tools_capability", lambda **_: None)
    monkeypatch.setattr(client.transport, "start", lambda: pytest.fail("refusal must not start"))
    result = client.call_tool(
        tool_name="computer_action", arguments=drag(x=-(10**400), y=2, to_x=3, to_y=4)
    )
    assert result.is_error is True and calls == []
    assert json.loads(result.extracted_text)["error"] == {
        "code": "invalid_request",
        "message": "x is invalid.",
        "dispatch": "not_dispatched",
    }
    owner.close()


def test_guard_refusal_reaches_the_model_as_an_is_error_tool_result(selected, monkeypatch):
    root, _, server, _, calls, _ = selected
    owner = manager(root, server)
    client = owner._build_live_client(server)
    monkeypatch.setattr(client, "_require_tools_capability", lambda **_: None)
    monkeypatch.setattr(client.transport, "start", lambda: pytest.fail("refusal must not start"))
    result = client.call_tool(
        tool_name="computer_action",
        arguments={"action": "key", "snapshot_id": "s", "element_id": "e1", "keys": ["f4"]},
    )
    assert result.is_error is True and calls == []
    error = json.loads(result.extracted_text)["error"]
    assert error == {
        "code": "invalid_keys",
        "message": "The requested key chord is unsupported.",
        "dispatch": "not_dispatched",
    }
    owner.close()


def passes(selected, monkeypatch, name, args):
    root, _, server, host, calls, _ = selected
    endpoint = channel(root, server, host)
    writes = []
    monkeypatch.setattr(endpoint, "start", lambda: None)
    monkeypatch.setattr(endpoint, "_send_serialized", lambda value: writes.append(value))
    marker = object()
    monkeypatch.setattr(endpoint, "_wait_for_response", lambda **_: marker)
    params = {"name": name, "arguments": args}
    assert endpoint.request(method="tools/call", params=params, timeout_s=1) is marker
    assert len(writes) == 1 and calls == []
    assert writes[0]["params"] == params


@pytest.mark.parametrize(
    "keys",
    [
        ["1", "0", "*", "6", "-", "8", "*", "1"],  # the live Calculator sequence
        ["2", "+", "2", "enter"],
        ["9", "/", "3", "return"],
        ["5", "%"],
        ["(", "1", ")"],
        ["backspace"],
        ["delete"],
        ["forward_delete"],
        ["tab"],
        ["escape"],
        ["esc"],
        ["home"],
        ["end"],
        ["left"],
        ["right"],
        ["up"],
        ["down"],
        ["space"],
        ["!"],
        ["?"],
        ["="],
        ["."],
        [","],
        ["Z"],
        [" Enter "],
        ["shift", "end"],
        ["shift", "!"],
        ["shift", "tab"],
        ["a"] * 8,
    ],
)
def test_printable_and_editing_keys_reach_the_helper(selected, monkeypatch, keys):
    passes(
        selected,
        monkeypatch,
        "computer_action",
        {"action": "key", "snapshot_id": "s", "element_id": "e1", "keys": keys},
    )


@pytest.mark.parametrize(
    "keys",
    [
        ["ctrl", "c"],
        ["ctrl", "a"],
        ["alt", "tab"],
        ["alt", "f4"],
        ["cmd", "q"],
        ["option", "x"],
        ["win", "r"],
        ["windows", "d"],
        ["meta", "l"],
        ["super", "e"],
        ["ctrl", "shift", "escape"],
        ["CTRL", "*"],
        ["shift", "control", "q"],
    ],
)
def test_modifier_chords_pass_to_the_helper_for_its_own_refusal(selected, monkeypatch, keys):
    # The helper answers these with high_impact_key_refused and never dispatches them.
    passes(
        selected,
        monkeypatch,
        "computer_action",
        {"action": "key", "snapshot_id": "s", "element_id": "e1", "keys": keys},
    )


@pytest.mark.parametrize(
    "args",
    [
        drag(element_id="e1", to_x=300, to_y=400),
        drag(element_id="e1", to_x=300.5, to_y=-4, duration_ms=0),
        drag(x=10, y=20, to_x=300, to_y=400, duration_ms=5000),
        drag(x=-100000, y=100000, to_x=0, to_y=0, duration_ms=300),
        drag(x=-100000.0, y=100000.0, to_x=0.0, to_y=-0.0),
    ],
)
def test_valid_drag_reaches_the_helper(selected, monkeypatch, args):
    passes(selected, monkeypatch, "computer_action", args)


@pytest.mark.parametrize(
    "name,args",
    [
        ("computer_state", {"bundle_id": "notepad.exe", "max_elements": 100}),
        ("computer_wait", {"duration_ms": 50}),
        *[
            ("computer_action", {"action": action, "snapshot_id": "s", "element_id": "e1"})
            for action in ("press", "focus", "click")
        ],
        (
            "computer_action",
            {"action": "type", "snapshot_id": "s", "element_id": "e1", "text": "x"},
        ),
        (
            "computer_action",
            {"action": "set_value", "snapshot_id": "s", "element_id": "e1", "text": ""},
        ),
        (
            "computer_action",
            {"action": "key", "snapshot_id": "s", "element_id": "e1", "keys": ["a"] * 8},
        ),
        (
            "computer_action",
            {"action": "key", "snapshot_id": "s", "element_id": "e1", "keys": ["shift", "tab"]},
        ),
        (
            "computer_action",
            {"action": "scroll", "snapshot_id": "s", "x": 10, "y": 20, "delta_y": 100},
        ),
        ("computer_action", {"action": "activate_application", "application": "Notepad"}),
    ],
)
def test_legacy_requests_keep_captured_parameters_before_effect(selected, monkeypatch, name, args):
    root, _, server, host, calls, _ = selected
    endpoint = channel(root, server, host)
    params = {"name": name, "arguments": args}
    expected = json.loads(json.dumps(params))
    starts, writes = [], []

    def start():
        starts.append(True)
        params["arguments"]["action"] = "quit_application"

    monkeypatch.setattr(endpoint, "start", start)
    monkeypatch.setattr(endpoint, "_send_serialized", lambda value: writes.append(value))
    marker = object()
    monkeypatch.setattr(endpoint, "_wait_for_response", lambda **_: marker)
    assert endpoint.request(method="tools/call", params=params, timeout_s=1) is marker
    assert starts == [True] and len(writes) == 1 and calls == []
    assert writes[0]["params"] == expected
    # This only measures original transport request/write routing; the inert
    # port supplies no native target/snapshot/activation or effect qualification.
