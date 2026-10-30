"""LR-40 denial controls retained under LR-41: Windows off, legacy Mac only."""

import io
import json
from types import SimpleNamespace

import pytest

from alysis_code import computer_use_platform_policy as policy
from alysis_code.agent.browser_start import build_browser_start_tool, build_computer_start_tools
from alysis_code.agent.computer_start_resolved import Entry
from alysis_code.agent.computer_use_tools import build_computer_use_tools
from alysis_code.agent.session import create_session
from alysis_code.agent.tool_definition import ToolDef
from alysis_code.agent.tool_discovery import install_tool_discovery, model_visible
from alysis_code.capabilities import build_capabilities_payload
from alysis_code.config import AppConfig, ConfigError
from alysis_code.embedded_browser_visual import (
    ComputerUseViewer,
    EmbeddedBrowserViewer,
    OpenPanelViewer,
)
from alysis_code.ide.health import capabilities_payload
from alysis_code.ide.prompt_queue import DurablePromptQueue
from alysis_code.ide.protocol import ProtocolError, ProtocolRequest
from alysis_code.ide.stdio_bridge import StdioBridge
from alysis_code.skills.computer_use import (
    computer_use_workflow_instructions,
    ordinary_entry_guidance,
)


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(policy.sys, "platform", "darwin")


@pytest.fixture
def simulated_windows(monkeypatch):
    # This fixture supplies no permission, target, or native admission grant.
    monkeypatch.setattr(policy, "sys", SimpleNamespace(platform="win32"))


def test_environment_cannot_override_real_policy(simulated_windows, monkeypatch):
    for key in ("ALYSIS_COMPUTER_USE_ENABLED", "ALYSIS_PLATFORM", "CU_PLATFORM", "OS"):
        monkeypatch.setenv(key, "win32")
    assert policy.computer_use_blocked()
    with pytest.raises(ValueError, match="unavailable|not qualified"):
        policy.refuse_computer_use()


@pytest.mark.parametrize(
    "field",
    [
        "sensitive_media",
        "browser_start_host_request",
        "computer_discovery",
        "computer_start_resolved_host",
        "computer_start_resolved_request",
        "computer_use_host_request",
        "computer_observation_available",
        "computer_interaction_v2",
        "computer_application_lifecycle_v1",
        "computer_window_selection_v1",
        "computer_use_verification_available",
        "computer_use_files_available",
        "computer_use_uploads_available",
        "computer_use_upload_objective",
    ],
)
def test_direct_session_before_workspace_provider(simulated_windows, tmp_path, field):
    root = tmp_path / "absent"
    with pytest.raises(ConfigError, match="unavailable|not qualified"):
        create_session(
            cfg=AppConfig(model="test"),
            root=root,
            mode="auto",
            yes=True,
            max_steps=1,
            no_log=True,
            **{field: True},
        )
    assert not root.exists()


@pytest.mark.parametrize(
    "params",
    [
        {"sensitive_media": True},
        {"computer_use_destination": {"platform": "win32"}},
        {"computer_use_host": {}},
        {"browser_start_host": {}},
        {"computer_start_resolved_host": {}},
        {"sensitive_media": False, "computer_use_host": {"supported": True, "enabled": True}},
    ],
)
def test_bridge_entry_before_factory(simulated_windows, tmp_path, params):
    calls = []
    out = io.StringIO()
    bridge = StdioBridge(
        stdout=out,
        prompt_queue=DurablePromptQueue(tmp_path / "queue.sqlite3"),
        create_session_fn=lambda **kw: calls.append(kw),
    )
    bridge.process_line(
        json.dumps(
            {"protocol_version": "1", "id": "blocked", "method": "session.create", "params": params}
        )
        + "\n"
    )
    reply = next(
        json.loads(x) for x in out.getvalue().splitlines() if json.loads(x).get("id") == "blocked"
    )
    if "computer_start_resolved_host" in params:
        # The existing schema rejects this malformed descriptor before dispatch.
        assert reply["error"]["code"] == "invalid_params"
        with pytest.raises(ProtocolError) as denied:
            bridge._session_create(
                ProtocolRequest(id="direct", method="session.create", params=params)
            )
        assert denied.value.code == "sensitive_media_restricted"
    else:
        assert reply["error"]["code"] == "sensitive_media_restricted"
    assert not calls and not bridge._sessions


@pytest.mark.parametrize(
    "method,code",
    [
        ("computer.respond", "computer_use_host_unavailable"),
        ("browser_start.respond", "browser_start_response_refused"),
        ("computer_start_resolved.respond", "computer_start_response_invalid"),
        ("task_budget.respond", "task_budget_refused"),
    ],
)
def test_late_reply_before_session_lookup(simulated_windows, method, code):
    bridge = StdioBridge.__new__(StdioBridge)
    bridge._role = "runtime"
    with pytest.raises(ProtocolError) as caught:
        bridge._dispatch(
            ProtocolRequest(id="old", method=method, params={"allow": True, "platform": "win32"})
        )
    assert caught.value.code == code and caught.value.request_id == "old"


def test_builders_and_direct_lower_bypass(simulated_windows):
    calls = []

    def request(*args):
        return calls.append(args)

    for operation in (
        lambda: build_browser_start_tool(request),
        lambda: build_computer_start_tools(request, {}),
        lambda: build_computer_use_tools(request),
        lambda: Entry({}, request, None, False, None),
        lambda: Entry.__new__(Entry).dispatch(None, {}, None, False, None),
        lambda: Entry.__new__(Entry).run({}),
        lambda: Entry.__new__(Entry).tool(),
        lambda: ComputerUseViewer(),
        lambda: ComputerUseViewer.__new__(ComputerUseViewer).capture({}),
    ):
        with pytest.raises(ValueError, match="unavailable|not qualified"):
            operation()
    assert calls == []
    bridge = StdioBridge.__new__(StdioBridge)
    for method, args in (
        (bridge._request_computer_use, (None, "computer_state", {})),
        (bridge._exchange_computer_use, (None, "computer_action", {})),
        (bridge._request_browser_start, (None, "https://example.com")),
        (bridge._request_resolved_start, (None, {}, {}, "old")),
    ):
        assert "error" in method(*args)
    with pytest.raises(ValueError, match="unavailable|not qualified"):
        bridge._write_transient_computer_use_request({})


def test_cached_tool_and_post_callback_refusal(monkeypatch, mac):
    calls = []
    tool = build_browser_start_tool(lambda url: calls.append(url) or {"allowed": True})
    cu = build_computer_use_tools(lambda *args: calls.append(args) or {"error": "inert"})
    monkeypatch.setattr(policy.sys, "platform", "win32")
    for action in (
        lambda: tool.run({"url": "https://example.com"}),
        lambda: cu["computer_state"].run({}),
    ):
        with pytest.raises(ValueError, match="unavailable|not qualified"):
            action()
    assert not calls
    monkeypatch.setattr(policy.sys, "platform", "darwin")

    def late(url):
        calls.append(url)
        monkeypatch.setattr(policy.sys, "platform", "win32")
        return {"allowed": True}

    tool = build_browser_start_tool(late)
    with pytest.raises(ValueError, match="unavailable|not qualified"):
        tool.run({"url": "https://example.com"})
    assert calls == ["https://example.com"]


def _tool(name, **metadata):
    return ToolDef(
        name=name,
        description="inert",
        parameters={"type": "object"},
        run=lambda _: {"ordinary": True},
        metadata=metadata,
    )


def test_discovery_capability_skill_and_ordinary_browser(simulated_windows, tmp_path):
    from alysis_code.skills import discover_skills

    host = _tool("browser_start", host_owned=True)
    ordinary = _tool("browser_start")
    tools = {
        "computer_state": _tool("computer_state"),
        "browser_start": ordinary,
        "file_read": _tool("file_read"),
    }
    install_tool_discovery(tools)
    assert "computer_state" not in tools and tools["browser_start"] is ordinary
    assert not model_visible(host) and model_visible(ordinary)
    assert ordinary.run({}) == {"ordinary": True}
    health = capabilities_payload()
    assert not (set(health["methods"]) & policy.CU_METHODS)
    assert not (set(health["events"]) & policy.CU_EVENTS)
    assert not (set(health["features"]) & policy.CU_FEATURES)
    features = build_capabilities_payload()["features"]
    assert "computer-use-host-v1" not in features
    assert features["embedded-browser-host-v1"] and features["github-mcp-plugin-v1"]
    assert computer_use_workflow_instructions() == policy.UNAVAILABLE_MESSAGE
    assert ordinary_entry_guidance(resolved=True) == policy.UNAVAILABLE_MESSAGE
    skills = discover_skills(
        focus_path=tmp_path,
        user_config_dir=tmp_path / "config",
        home_dir=tmp_path,
        cfg=AppConfig(model="test"),
    )
    assert "computer-use" not in skills.skills and skills.skills
    with StdioBridge.__new__(StdioBridge)._task_budget_context(
        SimpleNamespace(computer_use_host=None), object()
    ):
        pass


def test_ordinary_mac_viewers_reach_original_constructor(mac, monkeypatch):
    from alysis_code.assets.local_tools import LocalAssetViewer

    calls = []
    monkeypatch.setattr(LocalAssetViewer, "__init__", lambda self, **kw: calls.append(kw))
    EmbeddedBrowserViewer(ordinary=True)
    OpenPanelViewer(results=True)
    assert calls == [{"ordinary": True}, {"results": True}]
    ComputerUseViewer(protected=True)
    assert calls[-1] == {"protected": True} and len(calls) == 3


def test_legacy_mac_original_host_positive(mac, tmp_path):
    from test_ide_computer_use_host import test_host_request_is_transient_bound_and_one_shot

    assert build_capabilities_payload()["features"]["computer-use-host-v1"]
    assert "computer.respond" in capabilities_payload()["methods"]
    test_host_request_is_transient_bound_and_one_shot(tmp_path)


@pytest.mark.parametrize(
    "control",
    [
        "test_cancelled_job_cannot_emit_a_new_computer_use_request",
        "test_host_response_rejects_binding_mismatch_and_cancel_unblocks_waiter",
        "test_failed_host_response_wakes_waiter_with_exact_controller_error",
        "test_computer_action_requires_a_trusted_fresh_observation_before_host_dispatch",
    ],
)
def test_legacy_mac_original_host_negatives(mac, tmp_path, control):
    import test_ide_computer_use_host as controls

    fn = getattr(controls, control)
    if control.endswith("before_host_dispatch"):
        fn()
    else:
        fn(tmp_path)


def test_legacy_mac_browser_identity_positive(mac, tmp_path):
    from test_browser_start import test_acceptance_exact_identity_and_replay

    test_acceptance_exact_identity_and_replay(tmp_path)


@pytest.mark.parametrize("action", ["cancel", "close", "timeout", "job_changed"])
def test_legacy_mac_browser_stale_stopped_foreign(mac, tmp_path, action):
    from test_browser_start import test_pending_binding_expiry_and_cancellation

    test_pending_binding_expiry_and_cancellation(tmp_path, action)


@pytest.mark.parametrize(
    "name,metadata,blocked",
    [
        ("computer_state", {}, True),
        ("browser_start", {"host_owned": True}, True),
        ("ordinary_plugin_lookup", {"tool_type": "mcp"}, False),
    ],
)
def test_real_ordinary_turn_cannot_dispatch_cu_but_retains_ordinary(
    simulated_windows, tmp_path, monkeypatch, name, metadata, blocked
):
    from dataclasses import replace

    from test_read_ledger_delivery_boundary import _Client, _messages, _response, _session

    from alysis_code.llm.types import LLMResponse, ToolCall

    calls = []
    client = _Client(
        [
            _response(ToolCall(id="call", name=name, arguments={})),
            LLMResponse(content="Observed result.", raw={}, tool_calls=[]),
        ]
    )
    session = _session(tmp_path, monkeypatch, client)
    session.tools[name] = replace(
        _tool(name, **metadata), run=lambda args: calls.append(args) or {"ordinary": True}
    )
    try:
        session.run_turn("Use the supplied inspection tool to answer.")
        result = _messages(session)["call"]
        if blocked:
            expected = (
                "Sensitive tool operation failed; details redacted."
                if name == "computer_state"
                else "computer_use_host_unavailable"
            )
            assert calls == [] and result["error"] == expected
        else:
            assert calls == [{}] and result == {"ordinary": True}
    finally:
        session.close()


def test_ordinary_mac_managed_cdp_real_builder(mac, tmp_path):
    from test_managed_browser_agent_tools import (
        _build_tools,
        _FakeManagedBrowserService,
        _HostApprovalSurface,
    )

    service = _FakeManagedBrowserService()
    surface = _HostApprovalSurface()
    tools = _build_tools(tmp_path, service=service, surface=surface)
    assert not policy.is_computer_use_tool("browser_start", tools["browser_start"].metadata)
    result = tools["browser_start"].run({})
    assert len(service.calls) == 1 and service.calls[0][0] == "start"
    assert result["session_id"] == service.session_id
    assert len(surface.requests) == 1


def test_cached_cu_pixels_are_not_delivered_but_owned_cleanup_remains(simulated_windows):
    viewer = ComputerUseViewer.__new__(ComputerUseViewer)
    pixels = {"content": [{"image_url": {"url": "synthetic-pixels"}}]}
    viewer._pending = {"old": pixels}
    viewer._sensitive_inflight = []
    for operation in (
        viewer.status,
        lambda: viewer.take_visual_message("old", sensitive=True),
        lambda: viewer.view({"path": "must-not-be-read"}),
    ):
        with pytest.raises(ValueError, match="unavailable|not qualified"):
            operation()
    assert viewer._pending == {"old": pixels} and not viewer._sensitive_inflight
    viewer.clear_visual_messages()
    assert not viewer._pending and not pixels


def test_legacy_mac_original_viewer_delivery_and_cleanup(mac):
    viewer = ComputerUseViewer.__new__(ComputerUseViewer)
    pixels = {"content": [{"image_url": {"url": "synthetic-pixels"}}]}
    viewer._pending = {"id": pixels}
    viewer._sensitive_inflight = []
    assert viewer.take_visual_message("id", sensitive=True) is pixels
    assert viewer.take_visual_message("id") is None
    viewer.clear_visual_messages()
    assert not pixels and not viewer._sensitive_inflight
