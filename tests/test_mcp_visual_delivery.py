from __future__ import annotations

import base64
import io
import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from alysis_code.agent_loop import create_session
from alysis_code.cancellation import CooperativeCancellationError, EventCancellationToken
from alysis_code.config import AppConfig
from alysis_code.llm.types import LLMResponse, ToolCall
from alysis_code.mcp.client import McpToolCallResult, _normalize_content_items
from alysis_code.mcp.manager import McpToolBinding
from alysis_code.mcp.models import ResolvedMcpServer
from alysis_code.mcp.visual_delivery import McpVisualDelivery
from alysis_code.runtime_kind import RuntimeKind


def config(vision=True):
    cfg = AppConfig(model="mcp-vision-test", stream=False, routing_mode="code_only")
    cfg.web_search_mode = "off"
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {
                cfg.model: {
                    "supports_vision": vision,
                    "context_window_tokens": 128000,
                    "max_output_tokens": 1024,
                }
            }
        }
    }
    return cfg


def image_data():
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "purple").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def test_cli_import_does_not_cycle_through_mcp_visual_assets():
    result = subprocess.run(
        [sys.executable, "-c", "from alysis_code.cli import app"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "vision, data, mime",
    [(False, None, "image/png"), (True, "invalid", "image/png"), (True, None, "image/jpeg")],
)
def test_images_fail_explicitly_without_visual_delivery(vision, data, mime):
    channel = McpVisualDelivery(config(vision))
    receipt = channel.stage([{"data": data or image_data(), "mime_type": mime}], source="test")
    assert receipt["visual_delivered"] is False
    assert not channel._pending


def test_decompression_bomb_is_a_failed_receipt_not_an_uncaught_exception(monkeypatch):
    data = image_data()
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1)
    channel = McpVisualDelivery(config())
    receipt = channel.stage([{"data": data, "mime_type": "image/png"}], source="test")
    assert receipt["visual_delivered"] is False
    assert not channel._pending


@pytest.mark.parametrize(
    "data", [None, "invalid", "x" * 11_184_813], ids=["missing", "invalid", "oversized"]
)
def test_transport_rejected_images_produce_explicit_failure_without_pixels(data):
    content, summary, text = _normalize_content_items(
        server=ResolvedMcpServer(
            id="visual",
            transport="stdio",
            enabled=True,
            enabled_in=None,
            trust="explicit",
            allowed_tools=(),
            denied_tools=(),
            startup_timeout_s=5,
            call_timeout_s=5,
            tool_prefix=None,
        ),
        content_items=[{"type": "image", "mimeType": "image/png", "data": data}],
        stderr_tail="",
    )
    result = McpToolCallResult(False, None, content, summary, text, {})
    binding = McpToolBinding(
        server_id="visual",
        tool_name="observe",
        tool_alias="mcp__visual__observe",
        description="Observe",
        parameters={},
        client=SimpleNamespace(call_tool=lambda **kwargs: result),
        visual_delivery=McpVisualDelivery(config()),
    )
    payload = binding.run({})
    assert payload["visual_delivered"] is False
    assert payload["visual_error"] == "invalid_or_unsupported_image"
    assert payload["content"] == [{"type": "image", "summary": "image omitted"}]
    assert "delivered separately" not in json.dumps(payload)
    assert len(json.dumps(payload)) < 1000


def test_pending_images_remain_separate_and_are_all_cleared():
    channel = McpVisualDelivery(config())
    image = {"data": image_data(), "mime_type": "image/png"}
    first = channel.stage([image], source="test")
    second = channel.stage([image], source="test")
    request = channel.take_visual_message(first["visual_delivery_id"])
    assert request and len(request["content"]) == 2
    assert second["visual_delivery_id"] in channel._pending
    channel.clear_visual_messages()
    assert not channel._pending
    assert not channel._sensitive_inflight
    assert "data:image/" not in json.dumps(request)


@pytest.mark.parametrize("runtime", [RuntimeKind.INTERACTIVE_CHAT, RuntimeKind.ONE_SHOT])
@pytest.mark.parametrize("outcome", ["success", "provider_failure", "cancelled"])
def test_stdio_mcp_images_reach_one_model_request_without_persistence(
    tmp_path, monkeypatch, runtime, outcome
):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(cfg_dir))
    data = image_data()
    fixture = tmp_path / "fixture.json"
    fixture.write_text(
        json.dumps(
            {
                "tools_pages": [
                    [
                        {
                            "name": "observe",
                            "description": "Observe a visual surface.",
                            "inputSchema": {"type": "object", "properties": {}},
                            # This fixture only reads its synthetic image. Unknown
                            # MCP actions correctly need unified approval now.
                            "annotations": {"readOnlyHint": True},
                        }
                    ]
                ],
                "tool_call_results": {
                    "observe": {
                        "content": [
                            {"type": "text", "text": "A purple image."},
                            {"type": "image", "data": data, "mimeType": "image/png"},
                        ]
                    }
                },
            }
        )
    )
    server = Path(__file__).parent / "fixtures/mcp_servers/minimal_stdio_server.py"
    (cfg_dir / "mcp.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "servers": {
                    "visual": {
                        "transport": "stdio",
                        "command": sys.executable,
                        "args": [str(server)],
                        "env": {"ALYSIS_TEST_MCP_CONFIG": str(fixture)},
                        "trust": "explicit",
                    }
                },
            }
        )
    )
    session = create_session(
        cfg=config(),
        root=tmp_path,
        mode="act",
        yes=True,
        max_steps=5,
        no_log=False,
        api_key_override="test",
        runtime_kind=runtime,
        enable_compaction=False,
        session_log_dir_override=tmp_path / "logs",
    )

    cancelled = threading.Event()
    token = EventCancellationToken(cancelled)

    class Client:
        model = "mcp-vision-test"
        temperature = 0.2

        def __init__(self):
            self.counts = []
            self.parts = []

        def chat(self, *, messages, **kwargs):
            parts = [
                part
                for message in messages
                if isinstance(message.get("content"), list)
                for part in message["content"]
                if part.get("type") == "image_url"
            ]
            self.counts.append(len(parts))
            self.parts.extend(parts)
            if len(self.counts) == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[ToolCall(id="observe", name="mcp__visual__observe", arguments={})],
                    raw={},
                )
            if len(self.counts) == 2:
                assert parts, "The read-only fixture must deliver its image to this request."
                assert parts[0]["image_url"]["url"] == f"data:image/png;base64,{data}"
                if outcome == "provider_failure":
                    raise RuntimeError("Provider disconnected during image request")
                if outcome == "cancelled":
                    cancelled.set()
                    token.throw_if_cancelled()
                return LLMResponse(
                    content="Observed a purple square. I will check status before completing the task.",
                    tool_calls=[ToolCall(id="status", name="capability_status", arguments={})],
                    raw={},
                )
            assert any(
                message.get("role") == "assistant"
                and "Observed a purple square" in str(message.get("content", ""))
                for message in messages
            ), "task-relevant visual observations must survive when their pixels are scrubbed"
            return LLMResponse(content="Observed the image.", tool_calls=[], raw={})

    client = Client()
    session.client = client
    try:

        def run():
            return session.run_turn(
                "Use the visual MCP tool to inspect the image.", cancellation_token=token
            )

        if outcome == "provider_failure":
            with pytest.raises(RuntimeError, match="Provider disconnected"):
                run()
        elif outcome == "cancelled":
            with pytest.raises(CooperativeCancellationError):
                run()
        else:
            assert run() == 0
        assert client.counts == ([0, 1, 0] if outcome == "success" else [0, 1])
        assert all(not part for part in client.parts)
        assert data not in session.store.path.read_text()
        assert data not in json.dumps(session.messages)
        channel = session.tools["mcp__visual__observe"].visual_delivery
        assert not channel._pending
        assert not channel._sensitive_inflight
    finally:
        session.close()
