"""Platform admission and legacy workflow contracts with inert hosts."""

import copy
import io
import json
from types import SimpleNamespace

import pytest
from test_computer_discovery_recovery import _host, _inventory, _selection
from test_computer_start_resolved_turn import CORPUS
from test_computer_use_skill import _call, _ordinary_session
from test_computer_use_skill import isolated_paths as isolated_paths

from alysis_code import computer_use_platform_policy, token_budget
from alysis_code.agent.browser_start import BrowserStartHandoff
from alysis_code.ide.health import capabilities_payload
from alysis_code.ide.stdio_bridge import StdioBridge
from alysis_code.skills.computer_use import computer_use_workflow_instructions

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")


@pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
def test_resolved_start_capability_follows_the_host_platform(monkeypatch, platform):
    monkeypatch.setattr(computer_use_platform_policy, "sys", SimpleNamespace(platform=platform))
    assert ("computer_use_start_resolved_v1" in capabilities_payload()["features"]) is (
        platform == "darwin"
    )


@pytest.mark.parametrize("coverage", ["complete", "incomplete", "unavailable"])
def test_unsupported_host_rejects_descriptor_before_factory(tmp_path, monkeypatch, coverage):
    monkeypatch.setattr(computer_use_platform_policy, "sys", SimpleNamespace(platform="win32"))
    d = copy.deepcopy(CORPUS["bases"]["descriptor"])
    d["discovery"]["coverage"] = coverage
    if coverage != "complete":
        d["discovery"]["targets"] = []
    out = io.StringIO()
    calls = []
    bridge = StdioBridge(stdout=out, create_session_fn=lambda **kw: calls.append(kw))
    try:
        bridge.process_line(
            json.dumps(
                dict(
                    protocol_version="1",
                    id="create",
                    method="session.create",
                    params=dict(
                        session_id="stale",
                        workspace=str(tmp_path),
                        mode="review",
                        computer_start_resolved_host=d,
                    ),
                )
            )
        )
        reply = json.loads(out.getvalue().splitlines()[-1])
        assert reply["error"]["code"] == "sensitive_media_restricted"
        assert not calls and not bridge._sessions
        assert "computer_start_resolved_host" not in reply.get("result", {})
    finally:
        bridge.close()


def test_full_skill_read_is_legacy():
    skill = computer_use_workflow_instructions()
    assert "computer_start_resolved_v1" not in skill
    assert "call `computer_discover` before `computer_start`" in skill


@pytest.mark.parametrize("count", [2, 106])
def test_first_later_and_skill_read_requests_legacy_admission(tmp_path, monkeypatch, count):
    monkeypatch.setattr(token_budget, "_token_encoder", lambda: None)
    inventory = _inventory(count)
    plan = _selection(inventory, actions=[])
    admitted = []
    session, client = _ordinary_session(
        tmp_path,
        inventory,
        [
            _call("skill_read", {"name": "computer-use"}),
            _call("computer_discover", {}),
            _call("computer_start", plan),
        ],
        lambda selection: admitted.append(selection) or BrowserStartHandoff("fixture"),
    )
    try:
        assert (
            session.run_turn(
                "Read the computer-use skill then observe the selected app without input."
            )
            == 0
        )
        assert admitted == [_host(plan, inventory)]
        assert len(client.call_records) == 3
        for request in client.call_records:
            text = json.dumps(request)
            assert "computer_start_resolved_v1" not in text
            assert "computer_discover" in text and "computer_start" in text
            assert "Call computer_discover first" in text
        assert "call `computer_discover` before `computer_start`" in json.dumps(
            client.call_records[1]
        )
        assert session.last_turn_outcome["reason"] == "browser_start_handoff"
    finally:
        session.close()
