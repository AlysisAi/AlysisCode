"""GitHub sidecars are inert on untrusted and protected Computer Use startup."""

import io

import pytest
from test_computer_use_recovery import _ScriptedComputerUseClient
from test_computer_use_skill import isolated_paths as isolated_paths

from alysis_code import token_budget
from alysis_code.agent.computer_use_tools import COMPUTER_USE_TOOL_NAMES
from alysis_code.config import AppConfig
from alysis_code.ide import stdio_bridge
from alysis_code.llm.openai_compat import LLMResponse
from tests.test_github_discovery import BOOTSTRAP
from tests.test_github_repository_policy import payload
from tests.test_ide_host_actions import _request, _response_for

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")


@pytest.mark.parametrize("trusted,sensitive", [(False, False), (False, True), (True, True)])
@pytest.mark.parametrize("malformed", [False, True])
def test_policy_is_ignored_and_session_starts(tmp_path, monkeypatch, trusted, sensitive, malformed):
    monkeypatch.setenv("ALYSIS_API_KEY", "fixture")
    root = tmp_path / "workspace"
    root.mkdir()
    client = _ScriptedComputerUseClient([LLMResponse(content="done", raw={}, tool_calls=[])])
    monkeypatch.setattr("alysis_code.agent.session._make_session_llm_client", lambda **_: client)
    monkeypatch.setattr(token_budget, "_token_encoder", lambda: None)
    monkeypatch.setattr(
        stdio_bridge, "load_config", lambda: AppConfig(model="test-model", web_search_mode="off")
    )
    monkeypatch.setattr(stdio_bridge, "_checkpointing_supported", lambda _: False)
    monkeypatch.setattr(stdio_bridge, "_apply_computer_use_route_overrides", lambda *a, **k: None)
    destination = stdio_bridge._computer_use_destination(
        model="test-model",
        profile="fixture",
        auth_source="fixture",
        account_id="fixture",
        endpoint_digest="sha256:" + "a" * 64,
    )
    # The approved destination is an inert auth port; protected agent startup is real.
    monkeypatch.setattr(
        stdio_bridge, "_actual_computer_use_destination", lambda *a, **k: destination
    )
    host = stdio_bridge.ComputerUseHostBinding(
        operation_id="fixture",
        broker_fingerprint="sha256:" + "b" * 64,
        authority_epoch=1,
        scope_digest="sha256:" + "c" * 64,
    )
    params = dict(
        workspace=str(root),
        workspace_trusted=trusted,
        no_log=True,
        host_capabilities={"protocol_version": "1", "actions": ["mcp.github.call"]},
        host_mcp=BOOTSTRAP,
        github_repository_policy={"unexpected": "PRIVATE_SENTINEL"} if malformed else payload(),
    )
    if sensitive:
        params.update(
            sensitive_media=True,
            computer_use_destination=destination.to_protocol(),
            computer_use_host=host.to_protocol(),
        )
    out = io.StringIO()
    bridge = stdio_bridge.StdioBridge(stdout=out)
    try:
        bridge.process_line(_request("session.create", params, "create"))
        reply = _response_for(out, "create")
        assert reply["ok"], reply
        session = bridge._sessions[reply["result"]["session_id"]]
        assert session.github_repository_policy is None
        assert session.host_mcp is None and not session.host_actions
        assert "PRIVATE_SENTINEL" not in out.getvalue()
        if sensitive:
            assert set(session.agent_session.tools) == set(COMPUTER_USE_TOOL_NAMES)
            assert session.agent_session.sensitive_media
        assert client.calls == 0
    finally:
        bridge.close()
