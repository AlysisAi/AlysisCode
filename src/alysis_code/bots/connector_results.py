"""The desktop plugin's bounded reply, and the independent text and visual evidence for an Owl."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, JsonValue, StrictBool, field_validator

from .models import Input

CONNECTOR_RESULT_CAPABILITY = "owl_connector_results_v1"
CONNECTOR_REPLY_BYTES = 4 * 1024 * 1024
CONNECTOR_TEXT_BYTES = 256 * 1024
CONNECTOR_STRUCTURED_BYTES = 64 * 1024
CONNECTOR_IMAGE_BYTES = 1024 * 1024
CONNECTOR_IMAGE_PIXELS = 16_000_000
Count = Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]
_INLINE_IMAGE = re.compile(
    r"(?i:data:image/[a-z0-9.+-]+;base64,)[A-Za-z0-9+/=_-]*"
    r"|(?:iVBORw0KGgo|/9j/)[A-Za-z0-9+/=]{24,}"
)


class ConnectorOmissions(Input):
    text_bytes: Count = 0
    images: Count = 0
    blocks: Count = 0
    structured: StrictBool = False


class ConnectorImage(Input):
    model_config = ConfigDict(str_strip_whitespace=False)
    mime_type: Literal["image/png", "image/jpeg"]
    data: str = Field(max_length=((CONNECTOR_IMAGE_BYTES + 2) // 3) * 4, repr=False)


class ConnectorEvidence(Input):
    model_config = ConfigDict(str_strip_whitespace=False)
    version: int = Field(strict=True, ge=1, le=1)
    text: str = Field(max_length=CONNECTOR_TEXT_BYTES, repr=False)
    structured_content: dict[str, JsonValue] | None = Field(default=None, repr=False)
    image: ConnectorImage | None = Field(default=None, repr=False)
    omitted: ConnectorOmissions | None = None

    @field_validator("text")
    @classmethod
    def bounded_text(cls, value):
        if len(value.encode("utf-8")) > CONNECTOR_TEXT_BYTES:
            raise ValueError("Connector text exceeds its UTF-8 byte limit.")
        return value

    @field_validator("structured_content")
    @classmethod
    def bounded_structure(cls, value):
        if value is not None:
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            if len(raw.encode("utf-8")) > CONNECTOR_STRUCTURED_BYTES:
                raise ValueError("Structured connector content exceeds its UTF-8 byte limit.")
        return value


@dataclass(frozen=True)
class ConnectorResult:
    receipt: dict[str, Any] = field(repr=False)
    image_url: str | None = field(default=None, repr=False)
    width: int = 0
    height: int = 0


def _text_without_images(text: str, image_data: str) -> tuple[str, int]:
    text, removed = _INLINE_IMAGE.subn("[inline image omitted]", text)
    if len(image_data) >= 32 and image_data in text:
        removed += text.count(image_data)
        text = text.replace(image_data, "[inline image omitted]")
    return text, removed


def _structured_without_media(value, image_data):
    """Keep numeric fields, links and other usable evidence beside repeated media."""
    if isinstance(value, str):
        return _text_without_images(value, image_data)
    if isinstance(value, list):
        parts = [_structured_without_media(item, image_data) for item in value]
        return [item for item, _ in parts], any(removed for _, removed in parts)
    if isinstance(value, dict):
        mime = value.get("mimeType") or value.get("mime_type")
        media = value.get("type") in ("image", "audio") or (
            isinstance(mime, str) and mime.lower().startswith(("image/", "audio/"))
        )
        fields, omitted = {}, False
        for key, child in value.items():
            if _text_without_images(key, image_data)[1]:
                omitted = True
                continue
            if media and key in ("data", "blob") and isinstance(child, str):
                omitted = True
                continue
            fields[key], changed = _structured_without_media(child, image_data)
            omitted = omitted or changed
        return fields, omitted
    return value, False


def connector_result(
    plugin_id: str, plugin: str, tool: str, result: dict
) -> dict | ConnectorResult:
    """Preserve useful text even when the visual is unavailable or omitted."""
    receipt = {
        "plugin": plugin,
        "tool": tool,
        "is_error": bool(result.get("is_error")),
        "result": result.get("text", ""),
        "content_is_untrusted": True,
    }
    evidence = result.get("evidence")
    if evidence is None:
        if not plugin_id.startswith("hub."):
            # Old desktops drop nontext blocks and slice at 60,000 JavaScript UTF-16 units.
            text = receipt["result"]
            receipt["result"] = _text_without_images(text, "")[0]
            receipt["completeness"] = "unknown"
            if not text.strip():
                receipt["message"] = (
                    "The legacy desktop returned no readable evidence. It may have omitted media."
                )
            elif len(text.encode("utf-16-le", errors="surrogatepass")) >= 120000:
                receipt["message"] = (
                    "The legacy desktop reached its text limit. More content may have been cut off."
                )
            else:
                receipt["message"] = (
                    "The legacy desktop returned text only. Whether it omitted other content is unknown."
                )
            receipt["message"] += " Do not repeat the action to recover a result."
        return receipt

    receipt["result_version"] = 1
    omitted = {k: v for k, v in (evidence.get("omitted") or {}).items() if v}
    image = evidence.get("image")
    encoded_image = (image or {}).get("data", "")
    text = evidence["text"]
    # Some connectors echo their image in text or structuredContent. Only the validated
    # visual may carry pixels; the model's text and task archive keep a small omission.
    text, removed = _text_without_images(text, encoded_image)
    receipt["result"] = text
    if removed:
        omitted["blocks"] = omitted.get("blocks", 0) + removed
    structured = evidence.get("structured_content")
    if structured is not None:
        receipt["structured_content"], changed = _structured_without_media(
            structured, encoded_image
        )
        if changed:
            omitted["structured"] = True
    if omitted:
        receipt["omitted"] = omitted
        receipt["message"] = (
            "Some connector content was omitted. Use the retained evidence and disclose any relevant gap; do not repeat the action to recover it."
        )
    if image is None:
        if not text.strip() and structured is None and not omitted:
            receipt["message"] = (
                "The connector returned no content to inspect. Do not infer an outcome from empty output."
            )
        return ConnectorResult(receipt)

    # Use the Library's content sniffing, bounded decoding, orientation, downsampling and
    # metadata stripping. Never send the desktop's unverified bytes or follow a URL.
    from .documents import DocumentError, image_size, picture, sniff

    try:
        raw = base64.b64decode(encoded_image, validate=True)
        if (
            not raw
            or len(raw) > CONNECTOR_IMAGE_BYTES
            or image["mime_type"] != f"image/{sniff(raw[:16])}"
        ):
            raise ValueError
        width, height = image_size(raw)
        if width * height > CONNECTOR_IMAGE_PIXELS:
            raise ValueError
        decoded = picture(raw)
    except (ValueError, binascii.Error, DocumentError):
        receipt["visual_error"] = "invalid_or_unsupported_image"
        receipt["message"] = (
            "The returned image could not be shown. Use the retained evidence; do not invent visual details or repeat the action."
        )
        return ConnectorResult(receipt)
    if decoded.frames > 1:
        receipt["visual_frames"] = decoded.frames
        receipt["message"] = (
            "Only the first frame of the returned image is available. Other frames were omitted."
        )
    return ConnectorResult(receipt, decoded.data_url, decoded.width, decoded.height)
