"""Lead optional-plugin decision through real startup and first model request."""

import json
from types import SimpleNamespace

import pytest

from alysis_code.extensions.install import disable_plugin, install_plugin
from alysis_code.mcp.github_repository_policy import REPOSITORY_ACTIONS
from alysis_code.mcp.manager import McpToolBinding
from tests.test_github_discovery import ACTIONS, BOOTSTRAP
from tests.test_github_repository_policy_production import run_case, sidecar


@pytest.mark.parametrize(
    "state,policy_kind",
    [
        ("absent", "absent"),
        ("off", "absent"),
        ("off", "valid"),
        ("off", "malformed"),
        ("active", "valid"),
        ("active", "absent"),
        ("active", "malformed"),
    ],
)
def test_optional_plugin_guidance_in_actual_first_request(
    tmp_path, monkeypatch, state, policy_kind
):
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        "alysis_code.extensions.install._run_git", lambda *a, **k: pytest.fail("No network")
    )
    root = tmp_path / "workspace"
    root.mkdir()
    policy = "absent"
    if state != "absent":
        install_plugin(source="alysis.github", repo_root=root, trust_prompt=lambda _: True)
        if policy_kind == "valid":
            policy = sidecar(root)
        elif policy_kind == "malformed":
            policy = {"unexpected": "PRIVATE_POLICY_SENTINEL"}
        if state == "off":
            disable_plugin(plugin_id="alysis.github", repo_root=root)
    manager = SimpleNamespace(
        tool_bindings=[
            McpToolBinding(
                server_id="github",
                tool_name="own_tool",
                tool_alias="mcp__github__own_tool",
                description="Independent user tool",
                parameters={"type": "object", "properties": {}},
                client=SimpleNamespace(call_tool=lambda **_: pytest.fail("No tool invocation")),
            )
        ],
        close=lambda: None,
        startup_metadata=lambda: {},
        resolved_config=SimpleNamespace(has_any_config=False),
    )
    # This exact connector pair is sent even when no plugin is negotiated.
    actions = ["connectors.status", "connectors.github.repositories"]
    params = {}
    if state != "absent":
        actions += sorted(ACTIONS | {"github.repo.read"})
        params = dict(host_mcp=BOOTSTRAP, drop_inactive_host_plugins=True)
    invalid = state == "active" and policy_kind == "malformed"
    # Connected MCP schemas are deferred behind tool_search, so the scripted model
    # asks to load the user's own tool by exact name before its second request.
    load_own_tool = ("tool_search", {"names": ["mcp__github__own_tool"]})
    requests, host, wire, captured = run_case(
        root,
        monkeypatch,
        "responses",
        False,
        [load_own_tool],
        policy=policy,
        host_actions=actions,
        session_params=params,
        agent_mcp_manager=manager,
        expected_create_error="github_repository_policy_denied" if invalid else None,
    )
    assert host == []
    assert "PRIVATE_POLICY_SENTINEL" not in wire
    if invalid:
        assert requests == [] and "create_kwargs" not in captured
        return
    assert requests
    required = state == "active"
    assert captured["create_kwargs"]["github_repository_policy_required"] is required
    assert (
        "Do not bypass the GitHub plugin policy with shell commands" in json.dumps(requests[0])
    ) is required
    names = {t["name"] for t in requests[0]["tools"]}
    # The own tool is never sent eagerly; it is admitted (and discoverable) only
    # while the GitHub plugin is not active.
    assert "mcp__github__own_tool" not in names
    session = captured["session"]
    assert ("mcp__github__own_tool" in session.agent_session.tools) is not required
    if not required:
        assert "tool_search" in names
    assert len(requests) >= 2
    loaded = {t["name"] for t in requests[1]["tools"]}
    assert ("mcp__github__own_tool" in loaded) is not required
    if state != "active" or policy_kind == "absent":
        assert not session.host_actions & REPOSITORY_ACTIONS
        if state != "active":
            assert session.host_mcp is None
        assert session.github_repository_policy is None
        assert not any(n.startswith("github_") for n in names)
    else:
        assert session.github_repository_policy is not None
        assert session.host_mcp is not None
    if state == "off":
        assert '"plugin_id": "alysis.github"' in wire
