"""LR-42 real Mac W7 serving partition; no desktop/native effects or U2 grant."""

import copy
from types import SimpleNamespace

import pytest

from alysis_code import computer_use_platform_policy as policy
from alysis_code.agent.browser_start import BrowserStartHandoff, build_computer_start_tools
from alysis_code.agent.computer_use_tools import build_computer_use_tools
from alysis_code.ide import stdio_bridge as bridge_module
from alysis_code.ide.health import capabilities_payload
from alysis_code.ide.protocol import ProtocolError
from alysis_code.ide.stdio_bridge import ComputerUseHostBinding


@pytest.fixture(autouse=True)
def actual_mac_partition(monkeypatch):
    monkeypatch.setattr(policy, "sys", SimpleNamespace(platform="darwin"))


@pytest.mark.parametrize("platform", ["darwin", "win32", "linux", "Darwin"])
def test_exact_family_advertisement_no_environment_or_cached_qualification(monkeypatch, platform):
    from alysis_code.ide import health

    monkeypatch.setattr(policy, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(health, "COMPUTER_USE_START_RESOLVED_ENABLED", True)
    for name in ("ALYSIS_PLATFORM", "ALYSIS_COMPUTER_USE_ENABLED", "ALYSIS_MAC_W7_ENABLED"):
        monkeypatch.setenv(name, "darwin")
    caps = capabilities_payload()
    expected = {
        "computer_interaction_v2": 2,
        "computer_application_lifecycle_v1": 1,
        "computer_window_selection_v1": 1,
        "computer_use_start_resolved_v1": 1,
    }
    assert policy.mac_w7_computer_use_available() is (platform == "darwin")
    assert policy.w11_computer_use_qualified() is False
    with pytest.raises(ValueError, match="not qualified"):
        policy.refuse_unqualified_computer_use()
    for name, version in expected.items():
        if platform == "darwin":
            assert caps["features"][name] == {"supported": True, "version": version}
        else:
            assert name not in caps["features"]
    assert ("computer_start_resolved.respond" in caps["methods"]) is (platform == "darwin")
    assert ("computer_start_resolved_request" in caps["events"]) is (platform == "darwin")


def test_real_host_decoder_preserves_exact_versions_and_rejects_spoofed_fields():
    raw = ComputerUseHostBinding(
        operation_id="operation",
        broker_fingerprint="sha256:" + "a" * 64,
        authority_epoch=1,
        scope_digest="sha256:" + "b" * 64,
        interaction_version=2,
        application_lifecycle_version=1,
        window_selection_version=1,
    ).to_protocol()
    assert (
        bridge_module._computer_use_host_param(
            {"computer_use_host": raw}, request_id="ok"
        ).to_protocol()
        == raw
    )
    for field in policy.W11_HOST_FIELDS:
        for value in (None, True, 1.0, 3, "2"):
            altered = {**raw, field: value}
            with pytest.raises(ProtocolError):
                bridge_module._computer_use_host_param(
                    {"computer_use_host": altered}, request_id="bad"
                )
    for field in ("platform", "w11_qualified", "mac_w7_available", "same_engine"):
        with pytest.raises(ProtocolError):
            bridge_module._computer_use_host_param(
                {"computer_use_host": {**raw, field: True}}, request_id="bad"
            )


def test_resolved_original_callback_is_consumed_once_and_platform_rechecked(monkeypatch):
    from test_computer_start_resolved_contract import CORPUS, entry, invoke

    e, calls, cfg = entry()
    assert type(invoke(e, cfg, CORPUS["bases"]["plan"])) is BrowserStartHandoff
    assert len(calls) == 1 and e.retired == "consumed"
    from alysis_code.llm.types import LLMError

    with pytest.raises(LLMError, match="computer_start_capability_lost"):
        invoke(e, cfg, CORPUS["bases"]["plan"])
    assert len(calls) == 1

    def revoke(*_):
        monkeypatch.setattr(policy, "sys", SimpleNamespace(platform="win32"))
        return BrowserStartHandoff("original")

    e, calls, cfg = entry(host=revoke)
    with pytest.raises(ValueError, match="not qualified"):
        invoke(e, cfg, CORPUS["bases"]["plan"])
    assert len(calls) == 1 and e.retired != "consumed"


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "semantic"},
        {"action": "focused_input"},
        {
            "action": "type",
            "target_type": "focused_input",
            "input_mode": "foreground",
            "recipient_ref": "recipient-1",
            "text": "x",
        },
        {"action": "key", "target": "focused", "input_mode": "foreground", "keys": ["x"]},
    ],
)
def test_w7_is_not_new_action_or_foreground_authority(arguments):
    from test_computer_interaction_v2 import state

    calls = []

    def host(name, args):
        calls.append((name, copy.deepcopy(args)))
        return state(1)

    tools = build_computer_use_tools(host, interaction_v2=True, application_lifecycle_v1=True)
    assert "error" not in tools["computer_state"].run({})
    assert tools["computer_action"].run(arguments) == {"error": "computer_use_route_unqualified"}
    assert len(calls) == 1


def test_reopen_and_quit_plan_use_only_existing_selected_native_identity():
    from uuid import uuid4

    target = str(uuid4())
    calls = []
    discovery = {
        "version": 1,
        "application_lifecycle_version": 1,
        "targets": [
            {
                "target_id": target,
                "name": "Selected",
                "route": "native",
                "availability": "no_window",
            }
        ],
    }
    host = {
        "version": 1,
        "operation_id": "operation",
        "host_nonce": str(uuid4()),
        "discovery": discovery,
    }
    captured = bridge_module._browser_start_host_param(
        {"browser_start_host": host}, request_id="yes"
    )
    assert captured == host
    tools = build_computer_start_tools(
        lambda selection: calls.append(selection) or BrowserStartHandoff("once"),
        captured["discovery"],
    )
    plan = {
        "goal": "Close the selected app",
        "constraints": [],
        "operates_apps": True,
        "app_steps": [{"app": "Selected", "actions": ["activate_application", "quit_application"]}],
    }
    result = tools["computer_start"].run(plan)
    assert type(result) is BrowserStartHandoff and len(calls) == 1
    assert calls[0]["target_ids"] == [target]
    assert calls[0]["allowed_actions"] == ["activate_application", "quit_application"]
    assert discovery["targets"][0]["availability"] == "no_window"
    # Engine only transports the selected identity; native PID/reopen settlement is APP-owned.


def test_new_direct_bridge_arguments_need_original_negotiated_host(tmp_path):
    from test_ide_computer_use_host import _bridge_session

    bridge, out, session, job = _bridge_session(tmp_path)
    for name, args in [
        ("computer_state", {"interaction_version": 2}),
        ("computer_state", {"window_ref": "foreign"}),
        ("computer_action", {"action": "quit_application"}),
    ]:
        assert bridge._exchange_computer_use(session, name, args) == {
            "error": "computer_use_route_unqualified"
        }
    assert not out.getvalue() and not session.pending_computer_use
