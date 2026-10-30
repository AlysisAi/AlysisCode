from __future__ import annotations

from tests.test_ide_host_actions import _make_bridge


def test_missing_sidecar_withdraws_repository_actions(tmp_path, monkeypatch):
    out, bridge, sid, captured, _, _ = _make_bridge(
        tmp_path,
        monkeypatch,
        actions=("github.git.push", "connectors.github.repositories", "connectors.status"),
    )
    try:
        assert captured["host_action_capabilities"] == frozenset({"connectors.status"})
    finally:
        bridge.close()


def test_malformed_sidecar_refused_before_agent_creation(installed, monkeypatch):
    import io

    from alysis_code.config import AppConfig
    from alysis_code.ide import stdio_bridge
    from tests.test_github_repository_policy import payload
    from tests.test_ide_host_actions import _request, _response_for

    out = io.StringIO()
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="fixture"))

    def forbidden(**_):
        raise AssertionError("Invalid policy reached agent construction")

    bridge = stdio_bridge.StdioBridge(stdout=out, create_session_fn=forbidden)
    try:
        bridge.process_line(
            _request(
                "session.create",
                dict(
                    workspace=str(installed),
                    workspace_trusted=True,
                    github_repository_policy={**payload(), "unexpected": "PRIVATE_SENTINEL"},
                ),
                "invalid",
            )
        )
        response = _response_for(out, "invalid")
        assert not response["ok"] and response["error"]["code"] == "github_repository_policy_denied"
        assert "PRIVATE_SENTINEL" not in out.getvalue()
    finally:
        bridge.close()


def test_missing_sidecar_does_not_offer_catalog_or_direct_alias(installed, monkeypatch):
    import io
    from types import SimpleNamespace

    from alysis_code.agent.tools_assembly import build_tools
    from alysis_code.config import AppConfig
    from alysis_code.ide import stdio_bridge
    from tests.fixtures.github_host.bridge import FixtureSession
    from tests.test_github_discovery import ACTIONS, BOOTSTRAP
    from tests.test_host_actions import _store
    from tests.test_ide_host_actions import _request, _response_for

    out = io.StringIO()
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="fixture"))
    bridge = stdio_bridge.StdioBridge(stdout=out, create_session_fn=FixtureSession)
    try:
        bridge.process_line(
            _request(
                "session.create",
                dict(
                    workspace=str(installed),
                    workspace_trusted=True,
                    host_mcp=BOOTSTRAP,
                    host_capabilities=dict(protocol_version="1", actions=sorted(ACTIONS)),
                ),
                "create",
            )
        )
        result = _response_for(out, "create")["result"]
        assert "host_mcp" not in result
        session = bridge._sessions[result["session_id"]]
        assert not any(name.startswith("github_") for name in session.agent_session.tools)
    finally:
        bridge.close()
    tools = build_tools(
        root=installed,
        console=None,
        store=_store(installed),
        mode="auto",
        yes=True,
        cfg=AppConfig(model="fixture"),
        github_repository_policy_required=True,
        mcp_manager=SimpleNamespace(
            tool_bindings=[SimpleNamespace(tool_alias="mcp__github__impostor")]
        ),
    )
    assert "mcp__github__impostor" not in tools


pytest_plugins = ["tests.test_github_mcp_plugin"]
