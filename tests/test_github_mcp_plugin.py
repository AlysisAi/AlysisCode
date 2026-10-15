from __future__ import annotations

import copy
import hashlib
import io
import json
from types import SimpleNamespace

import pytest

from alysis_code.agent.errors import AgentRuntimeError
from alysis_code.agent.tools_assembly import build_tools
from alysis_code.config import AppConfig
from alysis_code.extensions.install import (
    disable_plugin,
    enable_plugin,
    install_plugin,
    uninstall_plugin,
)
from alysis_code.extensions.paths import installed_plugin_root
from alysis_code.extensions.workspace_trust import grant_workspace_trust
from alysis_code.host_actions import (
    HostActionError,
    normalize_host_action_arguments,
    normalize_host_action_result,
)
from alysis_code.ide import stdio_bridge
from alysis_code.ide.management_protocol import handle_management_method
from alysis_code.mcp.github_host import (
    GITHUB_MCP_ACTION,
    active_github_plugin,
    bind_host_mcp_catalog,
    normalize_host_mcp_catalog,
)
from alysis_code.surface import ApprovalDecision
from tests.fixtures.github_host.bridge import FixtureSession
from tests.test_host_actions import _ApprovalSurface, _build_tools, _store
from tests.test_ide_host_actions import _request, _respond, _response_for, _wait_for

CATALOG = {
    "protocol_version": 1,
    "plugin_id": "alysis.github",
    "server_id": "alysis.github/github",
    "tools": [
        {
            "name": "list_pull_requests",
            "description": "List pull requests.",
            "inputSchema": {"type": "object", "properties": {}},
            "readOnly": True,
        },
        {
            "name": "create_pull_request",
            "description": "Create a pull request.",
            "inputSchema": {
                "type": "object",
                "properties": {"title": {"type": "string"}},
                "required": ["title"],
            },
            "readOnly": False,
        },
    ],
}
RESULT = {"content": [{"type": "text", "text": "repository content"}], "isError": False}


@pytest.fixture
def installed(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(
        "alysis_code.extensions.install._run_git",
        lambda *_a, **_k: pytest.fail("Bundled install must not invoke git/network"),
    )
    root = tmp_path / "workspace"
    root.mkdir()
    install_plugin(source="alysis.github", repo_root=root, trust_prompt=lambda _r: True)
    return root


def test_bundled_install_uses_exact_two_step_review_and_no_python_mcp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(
        "alysis_code.extensions.install._run_git", lambda *_a, **_k: pytest.fail("No network")
    )
    params = {"workspace": str(tmp_path), "workspace_trusted": True, "source": "alysis.github"}
    review = handle_management_method("ext.install", params, request_id=1)
    assert review["action"]["kind"] == "requires_extension_trust_review"
    assert review["trust_request"]["source_kind"] == "bundled"
    assert review["trust_request"]["permissions_summary"]["host_managed_mcp_servers"] == ["github"]
    assert not active_github_plugin(tmp_path)
    approval = review["action"]["required_approval"]
    assert approval["source_url"].startswith("bundled://alysis.github/sha256/")
    rejected = {**approval, "manifest_sha256": "0" * 64}
    with pytest.raises(Exception, match="manifest_sha256"):
        handle_management_method(
            "ext.install", {**params, "yes": True, "trust_approval": rejected}, request_id=2
        )
    result = handle_management_method(
        "ext.install", {**params, "yes": True, "trust_approval": approval}, request_id=3
    )
    assert result["changed"]
    assert active_github_plugin(tmp_path)
    assert not (tmp_path / "config" / "mcp.json").exists()
    status = handle_management_method("ext.list", {"workspace": str(tmp_path)}, request_id=4)[
        "extensions"
    ][0]
    assert status["source_kind"] == "bundled"
    assert status["enabled_effective"]
    assert status["package_sha256"] == status["manifest_sha256"]


def test_plugin_lifecycle_content_integrity_and_dispatch_revocation(installed):
    bound = bind_host_mcp_catalog(installed, normalize_host_mcp_catalog(CATALOG))
    calls = []
    tools = _build_tools(
        installed,
        actions=(GITHUB_MCP_ACTION, "github.repo.read", "github.pr.create", "github.git.fetch"),
        host_mcp=bound,
        handler=lambda action, args: calls.append((action, args)) or RESULT,
        surface=_ApprovalSurface(ApprovalDecision(allow=False)),
    )
    assert "github_repo_read" not in tools and "github_pr_create" not in tools
    assert "github_git_fetch" in tools
    result = tools["mcp__github__list_pull_requests"].run({})
    assert "[MCP_UNTRUSTED_TEXT]" in result["text"]
    assert calls == [(GITHUB_MCP_ACTION, {"name": "list_pull_requests", "arguments": {}})]
    disable_plugin(plugin_id="alysis.github", repo_root=installed)
    with pytest.raises(AgentRuntimeError, match="disabled"):
        tools["mcp__github__list_pull_requests"].run({})
    assert len(calls) == 1
    enable_plugin(plugin_id="alysis.github", repo_root=installed)
    path = (
        installed_plugin_root("alysis.github", project=False, repo_root=installed)
        / "alysis-plugin.toml"
    )
    original = path.read_text()
    path.write_text(original + "\n# changed\n")
    assert active_github_plugin(installed) is None
    path.write_text(original)
    uninstall_plugin(plugin_id="alysis.github", repo_root=installed)
    assert active_github_plugin(installed) is None


def test_readonly_exposes_only_host_policy_read_tools_and_writes_require_host_authority(installed):
    bound = bind_host_mcp_catalog(installed, normalize_host_mcp_catalog(CATALOG))
    tools = _build_tools(
        installed,
        mode="readonly",
        actions=(GITHUB_MCP_ACTION,),
        host_mcp=bound,
        handler=lambda *_: RESULT,
    )
    assert "mcp__github__list_pull_requests" in tools
    assert "mcp__github__create_pull_request" not in tools
    tools = _build_tools(
        installed,
        actions=(GITHUB_MCP_ACTION,),
        host_mcp=bound,
        handler=lambda *_: pytest.fail("No host authority"),
    )
    with pytest.raises(AgentRuntimeError, match="Host-managed approval"):
        tools["mcp__github__create_pull_request"].run({"title": "Test"})


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c.update(plugin_id="other.plugin"),
        lambda c: c.update(token="secret"),
        lambda c: c["tools"].append(c["tools"][0]),
        lambda c: c["tools"][0].update(readOnly="true"),
        lambda c: c["tools"][0]["inputSchema"]["properties"].update(owner={"type": "string"}),
        lambda c: c["tools"][0].update(name="../escape"),
    ],
)
def test_invalid_catalogs_fail_closed(mutation):
    catalog = copy.deepcopy(CATALOG)
    mutation(catalog)
    with pytest.raises(HostActionError):
        normalize_host_mcp_catalog(catalog)


def test_call_protocol_rejects_targets_extra_fields_media_and_oversize_results():
    with pytest.raises(HostActionError):
        normalize_host_action_arguments(
            GITHUB_MCP_ACTION, {"name": "list_pull_requests", "arguments": {"owner": "other"}}
        )
    for result in (
        {"content": [], "token": "secret"},
        {"content": [{"type": "image", "data": "abc"}]},
        {"content": [{"type": "text", "text": "x" * 65536}]},
    ):
        with pytest.raises(HostActionError):
            normalize_host_action_result(GITHUB_MCP_ACTION, result)


@pytest.mark.parametrize("tool_name", ["list_pull_requests", "get_file_contents"])
def test_production_bridge_and_tool_assembly_round_trip(installed, monkeypatch, tool_name):
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="fixture"))
    out = io.StringIO()
    bridge = stdio_bridge.StdioBridge(stdout=out, create_session_fn=FixtureSession)
    catalog = copy.deepcopy(CATALOG)
    catalog["tools"][0]["name"] = tool_name
    arguments = {"path": "README.md"} if tool_name == "get_file_contents" else {}
    if arguments:
        catalog["tools"][0]["inputSchema"] = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }
    try:
        bridge.process_line(
            _request(
                "session.create",
                {
                    "workspace": str(installed),
                    "mode": "auto",
                    "workspace_trusted": True,
                    "host_capabilities": {
                        "protocol_version": "1",
                        "actions": [GITHUB_MCP_ACTION, "github.repo.read"],
                    },
                    "host_mcp": catalog,
                },
                "create",
            )
        )
        created = _response_for(out, "create")
        assert created["ok"], created
        session = created["result"]
        assert session["host_mcp"]["tool_names"] == [tool_name, "create_pull_request"]
        assert session["host_actions"]["actions"] == [GITHUB_MCP_ACTION]
        bridge.process_line(
            _request(
                "chat.send",
                {
                    "session_id": session["session_id"],
                    "message": json.dumps(
                        {"tool": f"mcp__github__{tool_name}", "arguments": arguments}
                    ),
                },
                "chat",
            )
        )
        event = _wait_for(out, lambda line: line.get("type") == "host_action_requested")
        assert event["payload"]["action"] == GITHUB_MCP_ACTION
        assert event["payload"]["arguments"] == {"name": tool_name, "arguments": arguments}
        _respond(bridge, event, request_id="response", result=RESULT)
        assert _response_for(out, "response")["ok"]
        message = _wait_for(out, lambda line: line.get("type") == "message_end")
        assert "MCP_UNTRUSTED_TEXT" in message["payload"]["text"]
    finally:
        bridge.close()


def test_desktop_never_falls_back_to_legacy_repo_pr_tools(installed, monkeypatch):
    monkeypatch.setenv("ALYSIS_DESKTOP_GITHUB_POLICY", "linked-only")
    tools = _build_tools(
        installed,
        actions=("github.repo.read", "github.pr.list", "github.pr.create", "github.git.fetch"),
        handler=lambda *_: pytest.fail("No API fallback"),
    )
    assert not {"github_repo_read", "github_pr_list", "github_pr_create"} & tools.keys()
    assert "github_git_fetch" in tools


def test_result_is_discarded_when_plugin_revoked_during_call(installed):
    bound = bind_host_mcp_catalog(installed, normalize_host_mcp_catalog(CATALOG))

    def handler(*_args):
        disable_plugin(plugin_id="alysis.github", repo_root=installed)
        return RESULT

    tools = _build_tools(installed, actions=(GITHUB_MCP_ACTION,), host_mcp=bound, handler=handler)
    with pytest.raises(AgentRuntimeError, match="disabled"):
        tools["mcp__github__list_pull_requests"].run({})


def test_manual_mcp_server_cannot_replace_selected_plugin_alias(installed):
    bound = bind_host_mcp_catalog(installed, normalize_host_mcp_catalog(CATALOG))
    impostor = SimpleNamespace(tool_alias="mcp__github__list_pull_requests")
    tools = build_tools(
        root=installed,
        console=None,
        store=_store(installed),
        mode="auto",
        yes=True,
        cfg=AppConfig(model="test-model"),
        host_action_capabilities=(GITHUB_MCP_ACTION,),
        host_mcp=bound,
        host_action_handler=lambda *_: RESULT,
        mcp_manager=SimpleNamespace(tool_bindings=[impostor]),
    )
    assert "MCP_UNTRUSTED_TEXT" in tools["mcp__github__list_pull_requests"].run({})["text"]


def test_bridge_refuses_disabled_plugin_before_agent_creation(installed, monkeypatch):
    disable_plugin(plugin_id="alysis.github", repo_root=installed)
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="fixture"))
    out = io.StringIO()
    bridge = stdio_bridge.StdioBridge(
        stdout=out,
        create_session_fn=lambda **_: pytest.fail("Inactive plugin reached agent bootstrap"),
    )
    try:
        bridge.process_line(
            _request(
                "session.create",
                {
                    "workspace": str(installed),
                    "mode": "auto",
                    "workspace_trusted": True,
                    "host_capabilities": {"protocol_version": "1", "actions": [GITHUB_MCP_ACTION]},
                    "host_mcp": CATALOG,
                },
                "create",
            )
        )
        response = _response_for(out, "create")
        assert response["error"]["code"] == "github_plugin_inactive"
    finally:
        bridge.close()


@pytest.mark.parametrize("action", ["github.git.fetch", "github.git.pull", "github.git.push"])
def test_bridge_revokes_native_git_tools_when_bound_plugin_is_disabled(
    installed, monkeypatch, action
):
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="fixture"))
    failures = []

    class CapturingSession(FixtureSession):
        def run_turn(self, message):
            try:
                return super().run_turn(message)
            except AgentRuntimeError as exc:
                failures.append(exc)
                self.surface.emit_message_end("refused")
                return 0

    out = io.StringIO()
    bridge = stdio_bridge.StdioBridge(stdout=out, create_session_fn=CapturingSession)
    try:
        bridge.process_line(
            _request(
                "session.create",
                {
                    "workspace": str(installed),
                    "mode": "auto",
                    "workspace_trusted": True,
                    "host_capabilities": {
                        "protocol_version": "1",
                        "actions": [GITHUB_MCP_ACTION, action],
                    },
                    "host_mcp": CATALOG,
                },
                "create",
            )
        )
        created = _response_for(out, "create")
        assert created["ok"], created
        disable_plugin(plugin_id="alysis.github", repo_root=installed)
        bridge.process_line(
            _request(
                "chat.send",
                {
                    "session_id": created["result"]["session_id"],
                    "message": json.dumps(
                        {
                            "tool": action.replace(".", "_"),
                            "arguments": {} if action.endswith("fetch") else {"branch": "main"},
                        }
                    ),
                },
                "chat",
            )
        )
        _wait_for(out, lambda line: line.get("type") == "message_end")
        assert len(failures) == 1
        assert isinstance(failures[0].__cause__, HostActionError)
        assert failures[0].__cause__.code == "github_plugin_revoked"
        assert all(
            json.loads(line).get("type") != "host_action_requested"
            for line in out.getvalue().splitlines()
        )
    finally:
        bridge.close()


def test_active_plugin_ignores_untrusted_project_fallback(installed):
    install_plugin(
        source="alysis.github", repo_root=installed, project=True, trust_prompt=lambda _r: True
    )
    user_manifest = (
        installed_plugin_root("alysis.github", project=False, repo_root=installed)
        / "alysis-plugin.toml"
    )
    user_manifest.write_text(user_manifest.read_text() + "\n# changed\n")
    assert active_github_plugin(installed) is None
    overrides = installed / ".alysis" / "extensions.json"
    grant_workspace_trust(
        repo_root=installed, overrides_sha256=hashlib.sha256(overrides.read_bytes()).hexdigest()
    )
    assert active_github_plugin(installed)["scope"] == "project"
    overrides.write_text(overrides.read_text() + "\n")
    assert active_github_plugin(installed) is None


@pytest.mark.parametrize("globally_enabled", [True, False])
def test_active_plugin_honors_only_trusted_project_enablement(installed, globally_enabled):
    if not globally_enabled:
        disable_plugin(plugin_id="alysis.github", repo_root=installed)
    overrides = installed / ".alysis" / "extensions.json"
    overrides.parent.mkdir(exist_ok=True)
    overrides.write_text(
        json.dumps({"disabled" if globally_enabled else "enabled": ["alysis.github"]})
    )
    assert bool(active_github_plugin(installed)) == globally_enabled
    grant_workspace_trust(
        repo_root=installed, overrides_sha256=hashlib.sha256(overrides.read_bytes()).hexdigest()
    )
    assert bool(active_github_plugin(installed)) != globally_enabled
    overrides.write_text(overrides.read_text() + "\n")
    assert bool(active_github_plugin(installed)) == globally_enabled
