from __future__ import annotations

import json

import pytest
from test_mcp_manager import _load_manager

from alysis_code.agent.connector_result_evidence import ConnectorLinkEvidence
from alysis_code.compaction.tool_output_offload import ToolOutputOffloader
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.session_artifacts import SessionArtifactLayout


@pytest.mark.parametrize("is_error", [False, True])
def test_bounded_mcp_state_remains_inline_without_duplicate_text(tmp_path, monkeypatch, is_error):
    # A small, paginated state can still cross the ordinary offload threshold if
    # the host repeats its escaped text. This applies to any MCP server.
    state = {
        "snapshot_id": "observed-42",
        "window": 'Disposable "document"',
        "elements": [f"e{i} Button press @10,{i * 25},80,20: Option {i}" for i in range(54)],
        "next_page_cursor": "observed-42:54",
    }
    raw_text = json.dumps(state)
    manager = _load_manager(
        tmp_path,
        monkeypatch,
        {
            "tools_pages": [[{"name": "observe", "inputSchema": {"type": "object"}}]],
            "tool_call_results": {
                "observe": {
                    "isError": is_error,
                    "content": [
                        {"type": "text", "text": raw_text},
                        {"type": "resource_link", "uri": "https://example.net/reference"},
                        {"type": "text", "text": "Last page is still available."},
                    ],
                    "structuredContent": {"page_count": 2},
                }
            },
        },
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
    )
    try:
        result = manager.tool_bindings[0].run({})
        serialized = json.dumps(result, ensure_ascii=True)
        offloader = ToolOutputOffloader(
            artifact_layout=SessionArtifactLayout(tmp_path / "session"),
            workspace_root=tmp_path,
            threshold_chars=6000,
            preview_chars=2000,
        )
        output = offloader.maybe_offload(
            tool_name="mcp__alpha__observe",
            tool_call_id="observation",
            step=1,
            result=result,
            content_json=serialized,
        )
        assert not output.offloaded, len(serialized)
        assert output.content_for_message == serialized
        assert serialized.count("observed-42:54") == 1
        assert result["is_error"] is is_error
        assert result["structured_content"] == {"page_count": 2}
        assert [item["type"] for item in result["content"]] == ["text", "resource_link", "text"]
        assert raw_text in result["content"][0]["text"]
        assert result["content"][2]["text"].startswith("[MCP_UNTRUSTED_TEXT]\n")
        assert "Last page is still available." in result["content"][2]["text"]
        evidence = ConnectorLinkEvidence()
        evidence.observe("mcp__alpha__observe", result)
        assert evidence.assess("https://example.net/reference")["unchecked_links"] == (
            [] if is_error else ["https://example.net/reference"]
        )
    finally:
        manager.close()
