from __future__ import annotations

import base64
import io
import json
from copy import deepcopy

import pytest
from PIL import Image

from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.embedded_browser_visual import EmbeddedBrowserViewer, validate_browser_image
from alysis_code.host_actions import normalize_host_action_result
from alysis_code.llm.openai_responses import _responses_input_from_messages
from alysis_code.llm.types import LLMResponse, ToolCall


def visual_cfg(supported=True):
    cfg = AppConfig(
        model="browser-visual-fixture",
        stream=False,
        routing_mode="code_only",
        web_search_mode="off",
    )
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {
                "browser-visual-fixture": {
                    "supports_vision": supported,
                    "context_window_tokens": 128000,
                    "max_output_tokens": 1024,
                }
            }
        }
    }
    return cfg


def image_result():
    output = io.BytesIO()
    Image.new("RGB", (20, 10), "blue").save(output, "PNG")
    return {
        "tab_id": "tab",
        "snapshot_id": "observed",
        "viewport": {"width": 200, "height": 100},
        "title": "Test page",
        "url": "https://example.com/",
        "image": {
            "mime_type": "image/png",
            "data": base64.b64encode(output.getvalue()).decode(),
            "width": 20,
            "height": 10,
        },
    }


def test_pixels_are_validated_delivered_once_and_never_returned_in_tool_text(tmp_path):
    viewer = EmbeddedBrowserViewer(root=tmp_path, cfg=visual_cfg())
    image = image_result()
    assert normalize_host_action_result("browser.screenshot", image) == image
    result = viewer.capture(image)
    assert image["image"]["data"] not in json.dumps(result)
    message = viewer.take_visual_message(result["visual_delivery_id"])
    assert message["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "untrusted data" in message["content"][1]["text"]
    assert result["viewport"] == {"width": 200, "height": 100}
    assert "xCSS = xImage" in message["content"][1]["text"]
    assert viewer.take_visual_message(result["visual_delivery_id"]) is None
    viewer.capture(image)
    viewer.clear_visual_messages()
    assert not viewer._pending


@pytest.mark.parametrize(
    "changes",
    [
        {"width": 2049},
        {"width": True},
        {"data": "x" * 60000},
        {"data": "!invalid!"},
        {"mime_type": "image/svg+xml"},
        {"mime_type": "image/jpeg"},
    ],
)
def test_invalid_or_unbounded_images_are_rejected(changes):
    image = image_result()["image"]
    with pytest.raises(ValueError):
        validate_browser_image({**image, **changes})


def test_encoded_dimensions_must_match_actual_pixels(tmp_path):
    viewer = EmbeddedBrowserViewer(root=tmp_path, cfg=visual_cfg())
    result = image_result()
    result["image"]["width"] = 40
    with pytest.raises(ValueError):
        viewer.capture(result)
    assert not viewer._pending


class VisualClient:
    model = "browser-visual-fixture"
    temperature = 0.2

    def __init__(self):
        self.calls = []

    def chat(self, *, messages, **kwargs):
        self.calls.append(deepcopy(messages))
        if len(self.calls) == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="browser-view",
                        name="embedded_browser_screenshot",
                        arguments={"tab_id": "tab"},
                    )
                ],
                raw={},
            )
        return LLMResponse(content="The browser screenshot was inspected.", tool_calls=[], raw={})


def test_real_session_delivers_image_to_provider_without_persisting_base64(tmp_path):
    session = create_session(
        cfg=visual_cfg(),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=False,
        api_key_override="test-key",
        session_log_dir_override=tmp_path / "sessions",
        enable_compaction=False,
        host_action_capabilities=("browser.screenshot",),
        host_action_handler=lambda action, args: image_result(),
    )
    client = VisualClient()
    session.client = client
    try:
        assert session.run_turn("Inspect the browser viewport.") == 0
        provider = _responses_input_from_messages(client.calls[1])
        parts = [p for msg in provider for p in msg.get("content", []) if isinstance(p, dict)]
        assert any(p.get("type") == "input_image" for p in parts)
        provider_call = next(
            c
            for m in client.calls[1]
            for c in m.get("tool_calls", [])
            if c.get("function", {}).get("name") == "embedded_browser_screenshot"
        )
        tool_message = next(
            m for m in client.calls[1] if m.get("tool_call_id") == provider_call["id"]
        )
        assert "base64" not in tool_message["content"]
        log_path = session.store.path
    finally:
        session.close()
    log = log_path.read_text()
    assert "asset_visual_delivered" in log
    assert image_result()["image"]["data"] not in log


def test_nonvisual_model_never_calls_screenshot_host(tmp_path):
    calls = []
    session = create_session(
        cfg=visual_cfg(False),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="test-key",
        enable_compaction=False,
        host_action_capabilities=("browser.screenshot",),
        host_action_handler=lambda action, args: calls.append(action) or image_result(),
    )
    try:
        assert "embedded_browser_screenshot" not in session.tools
        assert not calls
    finally:
        session.close()
