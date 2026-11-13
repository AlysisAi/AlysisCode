"""Image capability, delivery and limits; no provider calls."""

from __future__ import annotations

import base64
import copy
import io

import pytest
from PIL import Image

from alysis_code.bots.browser import BrowserImageResult
from alysis_code.bots.browser_images import (
    MAX_IMAGE_BYTES,
    append_browser_image,
    context_characters,
    declared_image_support,
)
from alysis_code.bots.store import LimitReached


def png_url(color="blue"):
    out = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(out, format="PNG")
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()


def result(source="source", color="blue"):
    return BrowserImageResult(
        {"source_id": source, "frame": {"width": 64, "height": 64}}, png_url(color)
    )


def test_declared_image_route_gate_and_unknown_text_only_fallback(monkeypatch):
    endpoint = "https://api.z.ai/api/coding/paas/v4"
    assert declared_image_support("glm-5.3-flash", endpoint)
    assert not declared_image_support("glm-5.3", endpoint)
    assert not declared_image_support("nonexistent-unknown-image-model", endpoint)
    monkeypatch.setenv("ALYSIS_SUPPORTS_VISION", "true")
    assert not declared_image_support("glm-5.3", endpoint)


def test_images_are_actual_content_blocks_and_old_pixels_are_not_resent():
    messages = [
        {"role": "system", "content": "Initial instructions"},
        {"role": "tool", "tool_call_id": "a", "content": "actual observation"},
    ]
    append_browser_image(messages, result("first"))
    first = copy.deepcopy(messages)
    assert messages[-1]["content"][1]["image_url"]["url"] == png_url()
    append_browser_image(messages, result("second", "red"))
    images = [
        p
        for m in messages
        if isinstance(m["content"], list)
        for p in m["content"]
        if p["type"] == "image_url"
    ]
    assert len(images) == 1 and images[0]["image_url"]["url"] == png_url("red")
    assert first[-1]["content"][1]["type"] == "image_url"
    assert messages[1]["content"] == "actual observation"
    assert "not a new user request" in messages[-1]["content"][0]["text"]
    assert context_characters(messages) < 1500


def test_image_count_bytes_encoding_and_remote_url_limits():
    messages = []
    append_browser_image(messages, result())
    with pytest.raises(LimitReached, match="one current"):
        context_characters(messages + messages)
    for url in [
        "https://remote.example/image.png",
        "data:image/png;base64,???",
        "data:image/png;base64," + base64.b64encode(b"x" * (MAX_IMAGE_BYTES + 1)).decode(),
    ]:
        bad = result()
        bad.image_url = url
        with pytest.raises(LimitReached):
            append_browser_image([], bad)
    assert len(messages) == 1


def test_large_real_png_does_not_consume_text_allowance_as_base64():
    import random

    pixels = random.Random(1).randbytes(256 * 256 * 3)
    out = io.BytesIO()
    Image.frombytes("RGB", (256, 256), pixels).save(out, format="PNG")
    large = result()
    large.image_url = "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()
    large.observation["frame"] = {"width": 256, "height": 256}
    messages = []
    append_browser_image(messages, large)
    assert len(large.image_url) > 64000
    assert context_characters(messages) < 1000
