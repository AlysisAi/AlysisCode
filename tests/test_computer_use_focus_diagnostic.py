import json
import time

import pytest

from alysis_code import computer_use_focus_diagnostic as diagnostic

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")


def state():
    return {
        "snapshot_id": "snapshot_00000000-0000-0000-0000-000000000000",
        "target": {"bundle_id": "com.apple.TextEdit", "window_title": "PRIVATE"},
        "focused_target": {"available": True, "key": True, "type": True, "role": "AXTextArea"},
        "elements": [{"role": "AXTextArea", "focused": True, "value": "PRIVATE"}],
    }


def test_projection_preserves_focus_and_origin_but_never_content():
    row = diagnostic.focus_diagnostic_projection(
        "decision_result",
        "computer_action",
        {"action": "type", "text": "PRIVATE", "target": "focused"},
        {"state": state()},
        origin="controller",
    )
    assert row["origin"] == "controller" and row["type"] and row["role"] == "AXTextArea"
    assert "PRIVATE" not in json.dumps(row)
    assert diagnostic.focus_diagnostic_projection("other", "computer_state", {}, state()) is None


def test_disabled_expired_and_broken_diagnostics_never_change_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostic, "diagnostic_root", lambda: tmp_path)
    diagnostic.record_focus_diagnostic("tool_projection", "computer_state", {}, state())
    assert not (tmp_path / "engine.jsonl").exists()
    (tmp_path / "enabled-until.txt").write_text(str(time.time() + 60))
    diagnostic.record_focus_diagnostic("tool_projection", "computer_state", {}, state())
    path = tmp_path / "engine.jsonl"
    assert json.loads(path.read_text())["type"]
    assert path.stat().st_mode & 0o777 == 0o600
    before = path.read_bytes()
    (tmp_path / "enabled-until.txt").write_text("0")
    diagnostic.record_focus_diagnostic("tool_projection", "computer_state", {}, state())
    assert path.read_bytes() == before
    (tmp_path / "enabled-until.txt").write_text("broken")
    diagnostic.record_focus_diagnostic("tool_projection", "computer_state", {}, state())
    assert path.read_bytes() == before


def test_focused_control_projection_does_not_authorize_foreground_input():
    from alysis_code.agent.computer_use_tools import build_computer_use_tools

    calls = []
    observed = state()
    observed["elements"] = [{"id": "list", "role": "AXList"}]

    def host(name, arguments):
        calls.append((name, dict(arguments)))
        if name == "computer_action":
            return {"receipt": {"action": "type", "performed": True}, "state": observed}
        return observed

    tools = build_computer_use_tools(host)
    result = tools["computer_state"].run({})
    assert result["focused_target"] == observed["focused_target"]
    assert not any(e["role"] == "AXTextArea" for e in result["elements"])
    result = tools["computer_action"].run(
        {"action": "type", "target": "focused", "input_mode": "foreground", "text": "fixture"}
    )
    assert result["error"] == "computer_use_route_unqualified"
    assert calls == [("computer_state", {})]
