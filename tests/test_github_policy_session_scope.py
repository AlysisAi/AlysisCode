"""A plugin repository policy must not become a global GitHub restriction."""

import json
from types import SimpleNamespace

import pytest

from alysis_code.agent.tools_assembly import build_tools
from alysis_code.config import AppConfig
from alysis_code.mcp.manager import McpToolBinding
from tests.test_github_repository_policy_production import run_case
from tests.test_host_actions import _store


@pytest.mark.parametrize(
    "actions",
    [
        [],
        ["tasks.list"],
        ["connectors.status"],
        ["github.repo.read"],
        ["connectors.status", "connectors.github.repositories"],
        ["connectors.status", "connectors.github.repositories", "terminal.list"],
    ],
)
@pytest.mark.parametrize("trusted", [False, True])
def test_first_model_request_and_user_mcp_are_scoped_to_negotiated_plugin(
    tmp_path, monkeypatch, actions, trusted
):
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "workspace"
    root.mkdir()
    requests, host, _, captured = run_case(
        root,
        monkeypatch,
        "responses",
        False,
        [],
        policy="absent",
        host_actions=actions,
        workspace_trusted=trusted,
    )
    expected = trusted and "github.repo.read" in actions
    assert (
        "Do not bypass the GitHub plugin policy with shell commands" in json.dumps(requests[0])
    ) is expected
    required = captured["create_kwargs"]["github_repository_policy_required"]
    assert required is expected
    assert host == []
    assert "connectors.github.repositories" not in captured["session"].host_actions
    manager = SimpleNamespace(
        tool_bindings=[
            McpToolBinding(
                server_id="github",
                tool_name="own_tool",
                tool_alias="mcp__github__own_tool",
                description="User configured tool",
                parameters={"type": "object", "properties": {}},
                client=SimpleNamespace(
                    call_tool=lambda **_: pytest.fail("No tool call in catalog test")
                ),
            )
        ]
    )
    tools = build_tools(
        root=root,
        console=None,
        store=_store(root),
        mode="auto",
        yes=True,
        cfg=AppConfig(model="test-model"),
        mcp_manager=manager,
        github_repository_policy_required=required,
    )
    assert ("mcp__github__own_tool" in tools) is not expected
