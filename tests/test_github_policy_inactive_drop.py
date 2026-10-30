"""Dropping inactive GitHub retains no policy or repository fallback authority."""

import io

import pytest

from alysis_code.extensions.install import disable_plugin
from alysis_code.ide.stdio_bridge import StdioBridge
from alysis_code.mcp.github_repository_policy import REPOSITORY_ACTIONS
from alysis_code.mcp.google_host import GOOGLE_MCP_ACTION
from tests.fixtures.github_host.bridge import FixtureSession
from tests.fixtures.github_repository_policy import explicit_all
from tests.test_github_discovery import BOOTSTRAP
from tests.test_google_mcp_plugin import catalogs
from tests.test_host_plugin_drop import installed  # noqa: F401 - shared P1 fixture
from tests.test_ide_host_actions import _request, _response_for


@pytest.mark.parametrize("policy_kind", ["all", "allow_list", "malformed"])
def test_dropped_plugin_retains_no_policy_catalog_or_repository_actions(installed, policy_kind):  # noqa: F811 - pytest fixture
    policy = explicit_all(installed)
    if policy_kind == "allow_list":
        policy.update(mode="allow_list", repositories=[{"owner": "acme", "repo": "allowed"}])
    elif policy_kind == "malformed":
        policy = {"not_a_policy": "PRIVATE_POLICY_SENTINEL"}
    disable_plugin(plugin_id="alysis.github", repo_root=installed)
    out = io.StringIO()
    bridge = StdioBridge(stdout=out, create_session_fn=FixtureSession)
    try:
        bridge.process_line(
            _request(
                "session.create",
                {
                    "workspace": str(installed),
                    "workspace_trusted": True,
                    "drop_inactive_host_plugins": True,
                    "host_capabilities": {
                        "protocol_version": "1",
                        "actions": sorted(REPOSITORY_ACTIONS | {GOOGLE_MCP_ACTION}),
                    },
                    "host_mcp": BOOTSTRAP,
                    "google_mcp": catalogs(),
                    "github_repository_policy": policy,
                },
                "create",
            )
        )
        reply = _response_for(out, "create")
        assert reply["ok"], reply
        session = bridge._sessions[reply["result"]["session_id"]]
        assert session.github_repository_policy is None
        assert session.host_mcp is None
        assert session.host_actions == frozenset({GOOGLE_MCP_ACTION})
        assert session.google_mcp
        assert "host_mcp" not in reply["result"]
        assert "PRIVATE_POLICY_SENTINEL" not in out.getvalue()
    finally:
        bridge.close()
