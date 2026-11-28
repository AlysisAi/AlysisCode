"""A synthetic desktop delivers bounded evidence for the original call through the real HTTP route."""

import base64
import io
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin
from pydantic import ValidationError
from test_bots_desktop_plugins import ASKS, CATALOG

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.connector_results import (
    CONNECTOR_REPLY_BYTES,
    CONNECTOR_STRUCTURED_BYTES,
    CONNECTOR_TEXT_BYTES,
    ConnectorResult,
    connector_result,
)
from alysis_code.bots.desktop_connectors import ResultInput
from alysis_code.cloud_connections import BoundConnections, ConnectionStore


def image_data(kind="PNG"):
    stream = io.BytesIO()
    meta = PngImagePlugin.PngInfo()
    meta.add_text("Comment", "Fixture location in image metadata")
    with Image.new("RGB", (32, 16), (243, 42, 18)) as source:
        source.save(stream, kind, **({"pnginfo": meta} if kind == "PNG" else {}))
    return base64.b64encode(stream.getvalue()).decode("ascii")


def evidence(text="", **parts):
    return {
        "ok": True,
        "is_error": False,
        "dispatch_state": "dispatched",
        "evidence": {"version": 1, "text": text, **parts},
    }


def deliver(body, plugin="alysis.reports"):
    data = ResultInput.model_validate(body)
    return connector_result(plugin, "Reports", "find", data.model_dump(exclude_none=True))


def test_claim_once_exact_ack_retry_mixed_image_and_long_tail(tmp_path):
    settings = Settings(tmp_path / "results.db", "t" * 40, "test", "https://model.invalid", "test")
    app = create_app(settings)
    client = TestClient(app)
    client.headers["Authorization"] = "Bearer " + settings.token
    assert "owl_connector_results_v1" in client.get("/v1/status").json()["capabilities"]
    assert client.put("/v1/desktop-plugins/catalog", json=CATALOG).status_code == 200
    hub = app.state.desktop_plugins
    bound = BoundConnections(
        ConnectionStore(tmp_path / "connections.db"), settings.owner, "bot:owl", None, hub
    )
    tool = bound.tools()[ASKS]
    original = "  Début\n" + "x" * 200000 + "\nFin 東京  "
    body = evidence(
        original,
        image={"mime_type": "image/png", "data": image_data()},
        structured_content={"totaux": {"mars": 42}, "note": "  oui  "},
        omitted={"images": 1, "blocks": 2, "text_bytes": 5},
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            bound.call_desktop,
            task={"bot_id": "owl", "id": "only-this-task"},
            tool=tool,
            arguments={"content": "fixture"},
            approval="person",
        )
        response = client.post("/v1/desktop-plugins/next", json={}).json()
        (call,) = response["calls"]
        assert call["result_version"] == 1 and call["task_id"] == "only-this-task"
        assert call["bot_id"] == "owl" and call["approval"] == "person"
        assert hub.next(wait=0)["calls"] == []
        path = f"/v1/desktop-plugins/calls/{call['id']}"
        assert client.post(path, json=body).json() == {"accepted": True}
        result = future.result(timeout=5)
    assert isinstance(result, ConnectorResult)
    assert result.receipt["result"] == original
    assert result.receipt["structured_content"] == body["evidence"]["structured_content"]
    assert result.receipt["omitted"] == body["evidence"]["omitted"]
    assert result.receipt["content_is_untrusted"] and not result.receipt["is_error"]
    assert result.receipt["result_version"] == 1
    assert (result.width, result.height) == (32, 16)
    assert body["evidence"]["image"]["data"] not in json.dumps(result.receipt)
    assert "data:image/" not in repr(result)
    shown = base64.b64decode(result.image_url.partition(",")[2])
    with Image.open(io.BytesIO(shown)) as image:
        assert image.getpixel((8, 4)) == (243, 42, 18) and "Comment" not in image.info
    assert client.post(path, json=body).json() == {"accepted": True}
    assert client.post(path, json=evidence("different evidence")).status_code == 409
    assert client.post(path.replace(call["id"], uuid4().hex), json=body).status_code == 404
    assert hub.next(wait=0)["calls"] == []


def test_visual_only_structured_only_empty_and_failed_visual_preserve_what_was_readable():
    visual = deliver(evidence(image={"mime_type": "image/jpeg", "data": image_data("JPEG")}))
    assert visual.image_url and visual.receipt["result"] == ""
    data = {"région": "Nord", "count": 0, "selected": False}
    result = deliver(evidence(structured_content=data))
    assert result.receipt["structured_content"] == data and result.image_url is None
    assert "no content" in deliver(evidence()).receipt["message"]
    assert deliver(evidence(structured_content={})).receipt["structured_content"] == {}
    broken = deliver(
        evidence(
            "Retained report",
            image={"mime_type": "image/png", "data": "not encoded image data"},
            omitted={"structured": True, "blocks": 1},
        )
    )
    assert broken.image_url is None and broken.receipt["result"] == "Retained report"
    assert broken.receipt["visual_error"] == "invalid_or_unsupported_image"
    assert broken.receipt["omitted"] == {"structured": True, "blocks": 1}
    wrong_mime = deliver(evidence(image={"mime_type": "image/png", "data": image_data("JPEG")}))
    assert wrong_mime.image_url is None and wrong_mime.receipt["visual_error"]
    returned_error = deliver({**evidence("Calendar is unavailable."), "is_error": True})
    assert (
        returned_error.receipt["is_error"]
        and returned_error.receipt["result"] == "Calendar is unavailable."
    )


def test_duplicated_pixels_are_excluded_from_structured_and_text_receipts():
    raw = image_data()
    out = deliver(
        evidence(
            f"Vue data:image/png;base64,{raw} — OK. {raw}",
            image={"mime_type": "image/png", "data": raw},
            structured_content={"thumbnail": raw, "total": 42},
        )
    )
    assert out.image_url and raw not in repr(out) and "data:image/" not in repr(out)
    assert raw not in json.dumps(out.receipt)
    assert out.receipt["result"] == "Vue [inline image omitted] — OK. [inline image omitted]"
    assert out.receipt["omitted"] == {"blocks": 2, "structured": True}
    assert out.receipt["structured_content"] == {"thumbnail": "[inline image omitted]", "total": 42}
    without_visual = deliver(
        evidence(
            structured_content={
                "rows": [
                    {"value": 42, "preview": raw},
                    {"type": "image", "data": "encoded pixels", "caption": "Chart"},
                ]
            }
        )
    )
    assert without_visual.image_url is None and "encoded pixels" not in json.dumps(
        without_visual.receipt
    )
    assert raw not in json.dumps(without_visual.receipt)
    assert without_visual.receipt["structured_content"]["rows"] == [
        {"value": 42, "preview": "[inline image omitted]"},
        {"type": "image", "caption": "Chart"},
    ]


def test_large_pixel_dimensions_are_omitted_even_when_png_transfer_is_small():
    stream = io.BytesIO()
    with Image.new("1", (4001, 4000)) as large:
        large.save(stream, "PNG")
    assert len(stream.getvalue()) < 65536
    data = base64.b64encode(stream.getvalue()).decode()
    result = deliver(
        evidence("The inventory total is 42.", image={"mime_type": "image/png", "data": data})
    )
    assert result.image_url is None and result.receipt["visual_error"]
    assert result.receipt["result"] == "The inventory total is 42."


def test_legacy_empty_mixed_and_overflow_have_explicit_uncertainty():
    for text, expected in [("", "no readable"), ("caption", "text only"), ("🟢" * 30000, "limit")]:
        legacy = deliver({"ok": True, "text": text})
        assert legacy["result"] == text and legacy["completeness"] == "unknown"
        assert legacy["content_is_untrusted"] and expected in legacy["message"]
    # More Apps has its own broker and does not use the desktop's old conversion.
    assert "completeness" not in deliver({"ok": True, "text": "ok"}, plugin="hub.reports")
    assert (
        "data:image/"
        not in deliver({"ok": True, "text": "data:image/png;base64," + image_data()})["result"]
    )


def test_independent_utf8_bounds_finite_structure_and_callback_errors(tmp_path):
    text = "é" * (CONNECTOR_TEXT_BYTES // 2)
    structured = {"x": "a" * (CONNECTOR_STRUCTURED_BYTES - len('{"x":""}'))}
    body = evidence(text, structured_content=structured)
    payload = ResultInput.model_validate(body)
    assert payload.evidence.text == text and payload.evidence.structured_content == structured
    for part in [
        {"text": text + "x"},
        {"structured_content": {"x": structured["x"] + "!"}},
        {"structured_content": {"x": float("inf")}},
        {"structured_content": []},
        {"version": True},
        {"version": 2},
        {"image": {"mime_type": "image/png", "data": "a" * (1400000)}},
        {"omitted": {"images": -1}},
        {"omitted": {"blocks": True}},
    ]:
        with pytest.raises(ValidationError):
            ResultInput.model_validate({**body, "evidence": {**body["evidence"], **part}})
    for change in [{"ok": False}, {"dispatch_state": "unknown"}, {"text": "two envelopes"}]:
        with pytest.raises(ValidationError):
            ResultInput.model_validate({**body, **change})
    settings = Settings(
        tmp_path / "limit.db", "t" * 40, "fixture", "https://model.invalid", "fixture"
    )
    client = TestClient(create_app(settings))
    path = "/v1/desktop-plugins/calls/" + uuid4().hex
    assert client.post(path, json=evidence()).status_code == 401
    client.headers["Authorization"] = "Bearer " + settings.token
    marker = "Private synthetic evidence"
    bad = client.post(
        path, json=evidence(marker, image={"mime_type": "text/plain", "data": marker})
    )
    assert bad.status_code == 422 and marker not in bad.text
    assert client.post(path, content=b" " * (CONNECTOR_REPLY_BYTES + 1)).status_code == 413
    # A reply larger than the ordinary request cap is accepted by this route only.
    escaped = evidence("\n" * CONNECTOR_TEXT_BYTES)
    assert client.post(path, json=escaped).status_code == 404
    assert client.post("/v1/desktop-plugins/next", json=escaped).status_code == 413
