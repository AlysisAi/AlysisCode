"""The desktop settings process keeps the ordinary catalog and its protocol checks."""

from __future__ import annotations

import io
import json

from alysis_code.ide.management_bridge import ManagementBridge
from alysis_code.ide.management_protocol import MANAGEMENT_METHODS
from alysis_code.ide.management_settings import SETTINGS_HANDLERS


def test_management_role_serves_workspace_skills_and_keeps_session_operations_out(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    folder = root / ".alysis_skills" / "local-example"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: local-example\ndescription: First description.\n---\nExample.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    frames = [
        ("initialize", {}),
        ("getCapabilities", {}),
        ("commands.list", {"mode": "chat"}),
        ("skill.list", {"workspace": str(root)}),
        ("skill.info", {"workspace": str(root), "name": "local-example"}),
        ("skill.list", {}),
        ("session.create", {}),
        ("profile.selectModel", {}),
        ("config.get", {"token": "example"}),
        ("bridge.shutdown", {}),
    ]
    reader = io.StringIO(
        "".join(
            json.dumps({"id": i, "protocol_version": "1", "method": m, "params": p}) + "\n"
            for i, (m, p) in enumerate(frames)
        )
    )
    out = io.StringIO()
    assert ManagementBridge(reader, out).run() == 0
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r["id"] for r in replies] == list(range(len(frames)))
    caps = replies[1]["result"]
    assert replies[0]["result"]["capabilities"] == caps
    assert set(SETTINGS_HANDLERS) <= set(MANAGEMENT_METHODS)
    assert "session.create" not in caps["methods"] and "skill.list" in caps["methods"]
    assert any(row["name"] == "compact" for row in replies[2]["result"]["commands"])
    assert any(row["name"] == "local-example" for row in replies[3]["result"]["skills"])
    assert replies[4]["result"]["skill"]["description"] == "First description."
    assert [r["error"]["code"] for r in replies[5:9]] == [
        "missing_param",
        "method_not_found",
        "workspace_trust_required",
        "inline_secret_rejected",
    ]
    assert replies[9]["ok"] is True


def test_management_role_finds_the_assistant_model_without_a_new_engine(tmp_path, monkeypatch):
    # The Assistant asks for its model before each ask; the settings process answers it, and
    # its model calls stay with an engine of their own.
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    frames = [
        ("getCapabilities", {}),
        ("assistant.observation.models", {}),
        ("assistant.observation.models", {"route": "example"}),
        ("assistant.observation.infer", {}),
    ]
    reader = io.StringIO(
        "".join(
            json.dumps({"id": i, "protocol_version": "1", "method": m, "params": p}) + "\n"
            for i, (m, p) in enumerate(frames)
        )
    )
    out = io.StringIO()
    assert ManagementBridge(reader, out).run() == 0
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    methods = replies[0]["result"]["methods"]
    assert "assistant.observation.models" in methods
    assert "assistant.observation.infer" not in methods
    found = replies[1]["result"]
    assert isinstance(found["models"], list) and found["quickLook"] is True
    assert {"activeModel", "modelStatus"} <= set(found)
    assert [r["error"]["code"] for r in replies[2:]] == ["invalid_params", "method_not_found"]
