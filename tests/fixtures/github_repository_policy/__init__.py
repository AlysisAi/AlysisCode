"""Synthetic explicit main-stored all policy for legacy preservation controls."""

from alysis_code.mcp.github_host import active_github_plugin


def explicit_all(root):
    return dict(
        version=1,
        plugin_id="alysis.github",
        host="github.com",
        plugin_manifest_sha256=active_github_plugin(root)["manifest_sha256"],
        account_login="fixture",
        account_link_revision="00000000-0000-4000-8000-000000000001",
        policy_revision="00000000-0000-4000-8000-000000000002",
        mode="all",
        repositories=[],
        workspace_repository=None,
    )
