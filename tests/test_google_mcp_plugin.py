from __future__ import annotations

import io
import json

import pytest

from alysis_code.agent.errors import AgentRuntimeError
from alysis_code.config import AppConfig
from alysis_code.extensions.install import (
    disable_plugin,
    enable_plugin,
    install_plugin,
    uninstall_plugin,
)
from alysis_code.extensions.paths import installed_plugin_root
from alysis_code.host_actions import HostActionError
from alysis_code.ide import stdio_bridge
from alysis_code.mcp.github_host import bind_host_mcp_catalog, normalize_host_mcp_catalog
from alysis_code.mcp.google_host import (
    GOOGLE_MCP_ACTION,
    SERVICES,
    active_google_plugin,
    bind_google_catalogs,
    normalize_google_catalogs,
)
from tests.fixtures.github_host.bridge import FixtureSession
from tests.test_github_mcp_plugin import CATALOG as GITHUB_CATALOG
from tests.test_github_mcp_plugin import RESULT
from tests.test_host_actions import _build_tools
from tests.test_ide_host_actions import _request, _respond, _response_for, _wait_for


def catalogs():
    return [
        {
            "protocol_version": 1,
            "service": service,
            "plugin_id": f"alysis.google_{service}",
            "server_id": f"alysis.google_{service}/{service}",
            "tools": [
                {
                    "name": "read_item",
                    "description": "Read",
                    "inputSchema": {"type": "object", "properties": {}},
                    "readOnly": True,
                },
                {
                    "name": "write_item",
                    "description": "Write",
                    "inputSchema": {"type": "object", "properties": {}},
                    "readOnly": False,
                },
            ],
        }
        for service in SERVICES
    ]


@pytest.fixture
def installed(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(
        "alysis_code.extensions.install._run_git",
        lambda *_a, **_k: pytest.fail("No network for bundled install"),
    )
    root = tmp_path / "workspace"
    root.mkdir()
    for service in SERVICES:
        install_plugin(
            source=f"alysis.google_{service}", repo_root=root, trust_prompt=lambda _: True
        )
    return root


def test_eight_independent_plugins_coexist_with_github_and_revoke(installed):
    install_plugin(source="alysis.github", repo_root=installed, trust_prompt=lambda _: True)
    bound = bind_google_catalogs(installed, normalize_google_catalogs(catalogs()))
    calls = []
    tools = _build_tools(
        installed,
        actions=(GOOGLE_MCP_ACTION, "mcp.github.call"),
        google_mcp=bound,
        host_mcp=bind_host_mcp_catalog(installed, normalize_host_mcp_catalog(GITHUB_CATALOG)),
        handler=lambda *a: calls.append(a) or RESULT,
    )
    assert "mcp__github__list_pull_requests" in tools
    for service in SERVICES:
        result = tools[f"mcp__google_{service}__read_item"].run({})
        assert "MCP_UNTRUSTED_TEXT" in result["text"]
        assert calls[-1] == (
            GOOGLE_MCP_ACTION,
            {"service": service, "name": "read_item", "arguments": {}},
        )
    disable_plugin(plugin_id="alysis.google_drive", repo_root=installed)
    with pytest.raises(AgentRuntimeError, match="disabled"):
        tools["mcp__google_drive__read_item"].run({})
    tools["mcp__google_docs__read_item"].run({})
    tools["mcp__github__list_pull_requests"].run({})
    enable_plugin(plugin_id="alysis.google_drive", repo_root=installed)
    path = (
        installed_plugin_root("alysis.google_drive", project=False, repo_root=installed)
        / "alysis-plugin.toml"
    )
    original = path.read_text()
    path.write_text(original + "\n# tampered\n")
    assert active_google_plugin(installed, "drive") is None
    path.write_text(original)
    uninstall_plugin(plugin_id="alysis.google_drive", repo_root=installed)
    assert active_google_plugin(installed, "drive") is None


def test_readonly_and_unapproved_writes(installed):
    bound = bind_google_catalogs(installed, normalize_google_catalogs(catalogs()))
    tools = _build_tools(
        installed,
        mode="readonly",
        actions=(GOOGLE_MCP_ACTION,),
        google_mcp=bound,
        handler=lambda *_: RESULT,
    )
    assert len([name for name in tools if name.startswith("mcp__google_")]) == 8
    tools = _build_tools(
        installed,
        actions=(GOOGLE_MCP_ACTION,),
        google_mcp=bound,
        handler=lambda *_: pytest.fail("Unapproved write"),
    )
    with pytest.raises(AgentRuntimeError, match="Host-managed approval"):
        tools["mcp__google_docs__write_item"].run({})


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.append(c[0]),
        lambda c: c[0].update(service="unknown"),
        lambda c: c[0].update(plugin_id="alysis.github"),
        lambda c: c[0].update(token="secret"),
        lambda c: c[0]["tools"][0].update(readOnly="true"),
        lambda c: c[0]["tools"][0].update(name="../escape"),
    ],
)
def test_bad_catalog_fails_closed(mutate):
    value = catalogs()
    mutate(value)
    with pytest.raises(HostActionError):
        normalize_google_catalogs(value)


@pytest.mark.parametrize("service", list(SERVICES))
def test_real_bridge_tool_roundtrip(installed, monkeypatch, service):
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="fixture"))
    out = io.StringIO()
    bridge = stdio_bridge.StdioBridge(stdout=out, create_session_fn=FixtureSession)
    try:
        bridge.process_line(
            _request(
                "session.create",
                {
                    "workspace": str(installed),
                    "mode": "auto",
                    "workspace_trusted": True,
                    "host_capabilities": {"protocol_version": "1", "actions": [GOOGLE_MCP_ACTION]},
                    "google_mcp": catalogs(),
                },
                "create",
            )
        )
        created = _response_for(out, "create")
        assert created["ok"], created
        session = created["result"]
        assert len(session["google_mcp"]) == 8
        bridge.process_line(
            _request(
                "chat.send",
                {
                    "session_id": session["session_id"],
                    "message": json.dumps(
                        {"tool": f"mcp__google_{service}__read_item", "arguments": {}}
                    ),
                },
                "chat",
            )
        )
        event = _wait_for(out, lambda line: line.get("type") == "host_action_requested")
        assert event["payload"]["action"] == GOOGLE_MCP_ACTION
        assert event["payload"]["arguments"]["service"] == service
        _respond(bridge, event, request_id="response", result=RESULT)
        assert _response_for(out, "response")["ok"]
        assert (
            "MCP_UNTRUSTED_TEXT"
            in _wait_for(out, lambda line: line.get("type") == "message_end")["payload"]["text"]
        )
    finally:
        bridge.close()


def test_revocation_discards_inflight_result(installed):
    bound = bind_google_catalogs(installed, normalize_google_catalogs(catalogs()))

    def handler(*_):
        disable_plugin(plugin_id="alysis.google_gmail", repo_root=installed)
        return RESULT

    tools = _build_tools(installed, actions=(GOOGLE_MCP_ACTION,), google_mcp=bound, handler=handler)
    with pytest.raises(AgentRuntimeError, match="disabled"):
        tools["mcp__google_gmail__read_item"].run({})
