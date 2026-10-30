"""LR-41: original Mac entry/host methods, no W11 or native qualification."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from alysis_code import computer_use_platform_policy as policy
from alysis_code.agent.browser_start import build_computer_start_tools
from alysis_code.agent.computer_start_resolved import Entry
from alysis_code.agent.computer_use_tools import build_computer_use_tools
from alysis_code.agent.session import create_session
from alysis_code.config import AppConfig, ConfigError
from alysis_code.ide import stdio_bridge as bridge_module
from alysis_code.ide.health import capabilities_payload
from alysis_code.ide.protocol import ProtocolError, ProtocolRequest
from alysis_code.ide.stdio_bridge import ComputerUseHostBinding, StdioBridge
from alysis_code.skills.computer_use import ordinary_entry_guidance


@pytest.fixture(autouse=True)
def mac(monkeypatch):
    monkeypatch.setattr(policy, "sys", SimpleNamespace(platform="darwin"))


@pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
def test_exact_platform_partition_no_environment_grant(monkeypatch, platform):
    monkeypatch.setattr(policy, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setenv("ALYSIS_PLATFORM", "darwin")
    monkeypatch.setenv("ALYSIS_COMPUTER_USE_ENABLED", "1")
    assert policy.legacy_mac_computer_use_available() is (platform == "darwin")
    assert not policy.w11_computer_use_qualified()
    caps = capabilities_payload()
    assert policy.mac_w7_computer_use_available() is (platform == "darwin")
    assert bool(policy.W11_FEATURES.intersection(caps["features"])) is (platform == "darwin")
    assert bool(policy.W11_METHODS.intersection(caps["methods"])) is (platform == "darwin")
    assert bool(policy.W11_EVENTS.intersection(caps["events"])) is (platform == "darwin")
    assert ("computer.respond" in caps["methods"]) is (platform == "darwin")
    assert ("browser_start.respond" in caps["methods"]) is (platform == "darwin")
    assert ("computer_use_host" in caps["features"]) is (platform == "darwin")


@pytest.mark.parametrize(
    "field",
    [
        "computer_start_resolved_host",
        "computer_start_resolved_request",
        "computer_interaction_v2",
        "computer_application_lifecycle_v1",
        "computer_window_selection_v1",
    ],
)
@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_new_session_flags_refuse_before_workspace_or_provider(
    tmp_path, field, monkeypatch, platform
):
    monkeypatch.setattr(policy, "sys", SimpleNamespace(platform=platform))
    root = tmp_path / "absent"
    with pytest.raises(ConfigError, match="not qualified"):
        create_session(
            cfg=AppConfig(model="test"),
            root=root,
            mode="auto",
            yes=True,
            max_steps=1,
            no_log=True,
            **{field: True},
        )
    assert not root.exists()


@pytest.mark.parametrize(
    "field", ["interaction_v2", "application_lifecycle_v1", "window_selection_v1"]
)
@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_new_tool_negotiation_cannot_be_requested_directly(field, monkeypatch, platform):
    monkeypatch.setattr(policy, "sys", SimpleNamespace(platform=platform))
    calls = []
    with pytest.raises(ValueError, match="unavailable"):
        build_computer_use_tools(lambda *a: calls.append(a), **{field: True})
    assert not calls


@pytest.mark.parametrize("field", sorted(policy.W11_HOST_FIELDS))
@pytest.mark.parametrize("value", [None, False, 1, 2])
@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_new_ipc_fields_cannot_grant_even_with_valid_legacy_binding(
    field, value, monkeypatch, platform
):
    monkeypatch.setattr(policy, "sys", SimpleNamespace(platform=platform))
    raw = ComputerUseHostBinding(
        operation_id="operation-legacy",
        broker_fingerprint="sha256:" + "a" * 64,
        authority_epoch=1,
        scope_digest="sha256:" + "b" * 64,
    ).to_protocol()
    assert (
        bridge_module._computer_use_host_param(
            {"computer_use_host": raw}, request_id="ok"
        ).to_protocol()
        == raw
    )
    raw[field] = value
    with pytest.raises(ProtocolError, match="not qualified"):
        bridge_module._computer_use_host_param({"computer_use_host": raw}, request_id="denied")


@pytest.mark.parametrize(
    "name,args",
    [
        ("computer_action", {"action": "quit_application"}),
        ("computer_action", {"action": "press", "target_type": "semantic", "element_id": "E"}),
        ("computer_action", {"action": "type", "target_type": "focused_input", "text": "secret"}),
        ("computer_action", {"action": "type", "target": "focused", "text": "secret"}),
        ("computer_action", {"action": "click", "input_mode": "foreground"}),
        ("computer_state", {"list_windows": False}),
        ("computer_state", {"window_ref": "old"}),
        ("computer_state", {"surface_ref": "old"}),
        ("computer_state", {"interaction_version": 2}),
    ],
)
def test_new_arguments_refuse_at_real_tool_and_direct_original_bridge(tmp_path, name, args):
    from test_ide_computer_use_host import _bridge_session

    calls = []
    tools = build_computer_use_tools(lambda *a: calls.append(a))
    result = tools[name].run(args)
    assert result.get("error") in {
        "computer_use_route_unqualified",
        "application_lifecycle_unsupported",
        "interaction_version_unsupported",
        "invalid_observation_arguments",
    }
    bridge, out, session, job = _bridge_session(tmp_path)
    for method in (bridge._request_computer_use, bridge._exchange_computer_use):
        assert method(session, name, args) == {"error": "computer_use_route_unqualified"}
    assert not calls and not out.getvalue() and not session.pending_computer_use


def test_v1_schema_does_not_offer_unqualified_targets_or_lifecycle():
    tools = build_computer_use_tools(lambda *a: {"error": "inert"}, observation_available=True)
    props = tools["computer_action"].parameters["properties"]
    assert not {"target", "target_type", "input_mode", "surface_ref", "recipient_ref"}.intersection(
        props
    )
    assert "quit_application" not in props["action"]["enum"]
    assert not {"window_ref", "list_windows", "surface_ref"}.intersection(
        tools["computer_state"].parameters["properties"]
    )
    assert "computer_start_resolved_v1" in ordinary_entry_guidance(resolved=True)


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_resolved_entry_and_reply_refuse_even_if_feature_constant_forged(monkeypatch, platform):
    monkeypatch.setattr(policy, "sys", SimpleNamespace(platform=platform))
    from alysis_code.ide import health

    monkeypatch.setattr(health, "COMPUTER_USE_START_RESOLVED_ENABLED", True)
    bridge = StdioBridge.__new__(StdioBridge)
    with pytest.raises(ProtocolError, match="not qualified"):
        bridge._session_create(
            ProtocolRequest(
                id="r", method="session.create", params={"computer_start_resolved_host": {}}
            )
        )
    for action in (
        lambda: Entry({}, None, None, False, None),
        lambda: Entry.__new__(Entry).run({}),
        lambda: Entry.__new__(Entry).dispatch(None, {}, None, False, None),
        lambda: Entry.__new__(Entry).tool(),
    ):
        with pytest.raises(ValueError, match="not qualified"):
            action()
    assert "error" in bridge._request_resolved_start(None, {}, {}, "A")
    with pytest.raises(ProtocolError):
        bridge._resolved_start_respond(
            ProtocolRequest(id="r", method="computer_start_resolved.respond", params={})
        )
    assert "computer_use_start_resolved_v1" not in capabilities_payload()["features"]


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_discovery_cannot_negotiate_lifecycle_or_native_synthetic_actions(monkeypatch, platform):
    monkeypatch.setattr(policy, "sys", SimpleNamespace(platform=platform))
    from uuid import uuid4

    from alysis_code.agent.browser_start import NATIVE_UNSUPPORTED_ACTIONS

    assert NATIVE_UNSUPPORTED_ACTIONS == {"type", "key", "scroll", "drag", "history_back"}
    calls = []
    discovery = {
        "version": 1,
        "targets": [{"target_id": str(uuid4()), "name": "LegacyApp", "route": "native"}],
    }
    with pytest.raises(ValueError, match="unavailable"):
        build_computer_start_tools(
            lambda *a: calls.append(a), {**discovery, "application_lifecycle_version": 1}
        )
    host = {
        "version": 1,
        "operation_id": "operation",
        "host_nonce": str(uuid4()),
        "discovery": {**discovery, "application_lifecycle_version": 1},
    }
    with pytest.raises(ProtocolError, match="not qualified"):
        bridge_module._browser_start_host_param({"browser_start_host": host}, request_id="denied")
    assert not calls


def test_original_legacy_host_read_after_stop_still_refuses(tmp_path):
    from test_ide_computer_use_host import test_cancelled_job_cannot_emit_a_new_computer_use_request

    test_cancelled_job_cannot_emit_a_new_computer_use_request(tmp_path)


@pytest.mark.parametrize("action", ["type", "key", "scroll", "drag", "history_back", "click"])
def test_native_synthetic_route_not_restored_through_v1_alias(action):
    from test_ide_computer_use_host import _lab_state

    calls = []

    def host(name, args):
        calls.append((name, args))
        return _lab_state("native-original", "READY")

    tools = build_computer_use_tools(host)
    assert tools["computer_state"].run({})["snapshot_id"] == "native-original"
    result = tools["computer_action"].run(
        {"action": action, "x": 1, "y": 1, "text": "literal", "keys": ["a"]}
    )
    assert result == {"error": "background_synthetic_input_unsupported"}
    assert len(calls) == 1


def test_same_engine_resolved_entry_cannot_downgrade_to_legacy_browser(tmp_path, monkeypatch):
    from test_ide_computer_use_host import _bridge_session

    bridge, out, session, job = _bridge_session(tmp_path)
    calls = []
    session.agent_session.resolved_entry = SimpleNamespace(
        choose_browser=lambda: calls.append("choose") or True
    )
    assert (
        bridge._request_browser_start(session, "https://example.com")["error"]
        == "browser_start_unavailable"
    )
    assert not calls and not out.getvalue() and session.pending_browser_start is None
    monkeypatch.setattr(policy, "sys", SimpleNamespace(platform="win32"))
    with pytest.raises(ValueError, match="not qualified"):
        bridge._write_resolved_wire({"type": "computer_start_resolved_request"})
    assert not out.getvalue()


def test_real_ordinary_turn_refuses_new_entry_before_its_callable(tmp_path, monkeypatch):
    from test_mac_computer_use_off import _tool
    from test_read_ledger_delivery_boundary import _Client, _messages, _response, _session

    from alysis_code.llm.types import LLMResponse, ToolCall

    calls = []
    client = _Client(
        [
            _response(ToolCall(id="new", name="computer_start_resolved_v1", arguments={})),
            LLMResponse(content="Unavailable.", raw={}, tool_calls=[]),
        ]
    )
    session = _session(tmp_path, monkeypatch, client)
    session.tools["computer_start_resolved_v1"] = replace(
        _tool("computer_start_resolved_v1"), run=lambda a: calls.append(a)
    )
    try:
        session.run_turn("Use the supplied tool.")
        assert not calls
        assert "error" in _messages(session)["new"]
    finally:
        session.close()
