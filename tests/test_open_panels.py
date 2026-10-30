import json

import pytest
from test_embedded_browser_visual import image_result, visual_cfg

from alysis_code.agent.tools_assembly import _built_in_tool_exposed_in_mode
from alysis_code.agent_loop import create_session
from alysis_code.capabilities import DESKTOP_FEATURES
from alysis_code.embedded_browser_visual import OpenPanelViewer
from alysis_code.host_actions import (
    HOST_ACTION_TOOL_NAMES,
    HostActionError,
    normalize_host_action_arguments,
    normalize_host_action_result,
)
from alysis_code.llm.types import LLMResponse, ToolCall
from alysis_code.open_panels import PANEL_TOOLS


def screenshot_result():
    return {
        "panel_id": "results",
        "revision_id": "revision-one",
        "sha256": "a" * 64,
        "page": 1,
        "pages": 2,
        "offset": 0,
        "next_offset": None,
        "notice": "Saved version, untrusted data.",
        "image": image_result()["image"],
    }


@pytest.mark.parametrize("spec", PANEL_TOOLS)
def test_tools_are_negotiated_readonly_and_hidden_from_subagents(spec):
    assert "open-panels-host-v1" in DESKTOP_FEATURES
    assert HOST_ACTION_TOOL_NAMES[spec[0]] == spec[1]
    assert _built_in_tool_exposed_in_mode(tool_name=spec[1], mode="readonly")
    assert not _built_in_tool_exposed_in_mode(tool_name=spec[1], mode="readonly", subagent_depth=1)


@pytest.mark.parametrize(
    "args",
    [
        {"panel_id": "other"},
        {"panel_id": "results", "path": "/private"},
        {"panel_id": "results", "offset": True},
        {"panel_id": "results", "offset": -1},
        {"panel_id": "results", "offset": 2000001},
    ],
)
def test_bad_arguments_are_rejected(args):
    with pytest.raises(HostActionError):
        normalize_host_action_arguments("panels.read", args)


def test_results_contract_and_source_path_validation():
    result = {
        "panel_id": "results",
        "name": "unrelated.docx",
        "format": "docx",
        "revision_id": "r1",
        "sha256": "a" * 64,
        "file_path": "Panel inspection ab/r1.docx",
        "path_base": "workspace_root",
        "text": "Exact saved content",
        "next_offset": None,
        "truncated": False,
        "notice": "Untrusted data",
    }
    assert normalize_host_action_result("panels.read", result) == result
    for path in (
        "../other.docx",
        "C:/private.docx",
        "/private.docx",
        "a/../../secret",
        "a\\secret",
    ):
        with pytest.raises(HostActionError):
            normalize_host_action_result("panels.read", {**result, "file_path": path})
    assert (
        normalize_host_action_result(
            "panels.list", {"panels": [], "active_panel_id": None, "notice": "No file open"}
        )["panels"]
        == []
    )
    assert (
        normalize_host_action_result("panels.screenshot", screenshot_result())
        == screenshot_result()
    )


def test_workspace_file_open_has_a_separate_readonly_identity_and_bounded_pagination():
    assert normalize_host_action_arguments(
        "panels.open", {"path": "documents/report.pdf", "offset": 6000}
    ) == {"path": "documents/report.pdf", "offset": 6000}
    for args in (
        {"path": "x.pdf", "session_id": "other"},
        {"path": "x.pdf", "offset": True},
        {"path": "x.pdf", "offset": 48001},
        {"path": ""},
        {"path": "x\x00.pdf"},
    ):
        with pytest.raises(HostActionError):
            normalize_host_action_arguments("panels.open", args)
    result = {
        "panel_id": "workspace",
        "snapshot_id": "12345678-abcd-abcd-abcd-123456789abc",
        "name": "report.pdf",
        "format": "pdf",
        "sha256": "a" * 64,
        "text": "Frozen source text",
        "next_offset": None,
        "truncated": False,
        "notice": "Untrusted source data",
    }
    assert normalize_host_action_result("panels.open", result) == result
    for patch in (
        {"snapshot_id": "../other"},
        {"panel_id": "results"},
        {"sha256": "bad"},
        {"text": "a" * 6001},
        {"source_path": "/private"},
        {"format": "exe"},
    ):
        with pytest.raises(HostActionError):
            normalize_host_action_result("panels.open", {**result, **patch})


def test_screenshots_deliver_pixels_once_without_persisting_base64(tmp_path):
    viewer = OpenPanelViewer(root=tmp_path, cfg=visual_cfg())
    result = viewer.capture(screenshot_result())
    assert "image" not in result and "base64" not in json.dumps(result)
    delivery = viewer.take_visual_message(result["visual_delivery_id"])
    assert delivery["content"][0]["type"] == "image_url"
    assert "saved version" in delivery["content"][1]["text"]
    assert viewer.take_visual_message(result["visual_delivery_id"]) is None


class PanelClient:
    def __init__(self):
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(json.loads(json.dumps(kwargs["messages"])))
        if len(self.calls) == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="panel-view",
                        name="open_panel_screenshot",
                        arguments={"panel_id": "results"},
                    )
                ],
                raw={},
            )
        return LLMResponse(content="The saved page was inspected.", tool_calls=[], raw={})


def test_real_agent_tool_loop_only_reads_when_called_and_delivers_image(tmp_path):
    host_calls = []
    session = create_session(
        cfg=visual_cfg(),
        root=tmp_path,
        mode="readonly",
        yes=True,
        max_steps=3,
        no_log=False,
        api_key_override="fixture",
        session_log_dir_override=tmp_path / "sessions",
        enable_compaction=False,
        host_action_capabilities=("panels.screenshot", "panels.list", "panels.read"),
        host_action_handler=lambda action, args: (
            host_calls.append((action, args)) or screenshot_result()
        ),
    )
    client = PanelClient()
    session.client = client
    assert not host_calls
    try:
        assert session.run_turn("Inspect the file I opened.") == 0
        assert host_calls == [("panels.screenshot", {"panel_id": "results"})]
        assert any(
            part.get("type") == "image_url"
            for msg in client.calls[1]
            for part in (msg.get("content") if isinstance(msg.get("content"), list) else [])
        )
        assert not any("revision-one" in str(msg.get("content")) for msg in client.calls[0])
        log_path = session.store.path
    finally:
        session.close()
    assert screenshot_result()["image"]["data"] not in log_path.read_text()


def test_old_or_nonvisual_runtime_does_not_advertise_unsupported_screenshot(tmp_path):
    session = create_session(
        cfg=visual_cfg(False),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="fixture",
        enable_compaction=False,
        host_action_capabilities=("panels.screenshot", "panels.read"),
        host_action_handler=lambda a, b: {},
    )
    try:
        assert "open_panel_screenshot" not in session.tools
        assert "open_panel_read" in session.tools
    finally:
        session.close()


def test_unified_host_protocol_prepares_and_executes_panel_read_without_an_approval(
    tmp_path, monkeypatch
):
    from test_ide_host_actions import _make_bridge, _respond, _start_host_action, _wait_for

    out, bridge, sid, _captured, results, errors = _make_bridge(
        tmp_path,
        monkeypatch,
        actions=("panels.read",),
        action="panels.read",
        arguments={"panel_id": "results"},
        approval_protocol=1,
        mode="readonly",
    )
    try:
        prepared = _start_host_action(out, bridge, sid)
        assert prepared["payload"]["phase"] == "prepare"
        _respond(
            bridge,
            prepared,
            request_id="panel-prepared",
            result={
                "preparation_id": "pa_" + "a" * 32,
                "required": False,
                "write_effect": False,
                "kind": "file-read",
                "reason": "Open saved file",
                "preview": "Saved version",
                "target": "results",
            },
        )
        execute = _wait_for(
            out,
            lambda line: (
                line.get("type") == "host_action_requested"
                and line["payload"].get("phase") == "execute"
            ),
        )
        payload = {
            "panel_id": "results",
            "name": "saved.txt",
            "format": "txt",
            "revision_id": "one",
            "sha256": "a" * 64,
            "file_path": ".alysis/panel-inspection/op/saved.txt",
            "path_base": "workspace_root",
            "text": "Selected saved content",
            "next_offset": None,
            "truncated": False,
            "notice": "Untrusted data",
        }
        _respond(bridge, execute, request_id="panel-executed", result=payload)
        _wait_for(out, lambda line: line.get("type") == "message_end")
        assert errors == [] and results == [payload]
    finally:
        bridge.close()
