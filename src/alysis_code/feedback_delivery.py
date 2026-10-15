"""Prepare an immutable, reviewable report and deliver it to private Alysis intake."""

from __future__ import annotations

import json
import platform
from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from . import __version__, alysis_cloud
from .config import load_persisted_profile_keys
from .feedback_report import FeedbackReportError, _redact_jsonable, sanitize_feedback_text

MAX_REQUEST_BYTES = 262144
MAX_DESCRIPTION_CHARS = 8000


@dataclass(frozen=True)
class PreparedFeedback:
    report_id: str
    endpoint: str
    body: bytes = field(repr=False)

    @cached_property
    def _preview_text(self) -> str:
        return json.dumps(json.loads(self.body), ensure_ascii=False, indent=2)

    def preview(self) -> str:
        return self._preview_text


class FeedbackDeliveryError(FeedbackReportError):
    """The service refused the report; ``status_code`` says why, for hosts that word it."""

    def __init__(self, message: str, *, status_code: int, signed_in: bool) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.signed_in = signed_in


@dataclass(frozen=True)
class FeedbackReceipt:
    report_id: str
    created_at: str
    anonymous: bool = False


def has_alysis_sign_in() -> bool:
    """True when reports go out tied to the user's Alysis account, else anonymously."""
    key = load_persisted_profile_keys().get(alysis_cloud.PROFILE_KEY) or ""
    return isinstance(key, str) and key.startswith("slk_")


def _checked_endpoint(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    if (
        not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or not (
            parts.scheme == "https"
            or (parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost", "::1"})
        )
    ):
        raise FeedbackReportError("Feedback requires an HTTPS Alysis endpoint.")
    return endpoint


def prepare_feedback(
    *,
    workspace_root: Path,
    feedback_text: str,
    include_diagnostics: bool,
    active_session: Any | None = None,
) -> PreparedFeedback:
    description = sanitize_feedback_text(feedback_text.strip(), workspace_root=workspace_root)
    if not description:
        raise FeedbackReportError("Add a description before sending feedback.")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise FeedbackReportError(
            "Keep the description under 8,000 characters, or save it locally."
        )
    diagnostics = None
    if include_diagnostics:
        # Deliberately bounded: no retained sessions, file trees, images or ZIP uploads.
        messages = getattr(active_session, "messages", []) or []
        selected: list[Any] = []
        size = 0
        for message in reversed(messages[-20:]):
            if isinstance(message, dict):
                message = {
                    k: v
                    for k, v in message.items()
                    if k in {"role", "content", "tool_calls", "tool_call_id", "name"}
                }
                if isinstance(message.get("content"), list):
                    message["content"] = [
                        {"type": "text", "text": part["text"]}
                        for part in message["content"]
                        if isinstance(part, dict)
                        and part.get("type") in {"text", "input_text", "output_text"}
                        and isinstance(part.get("text"), str)
                    ]
            clean = _redact_jsonable(message, workspace_root=workspace_root)
            encoded = json.dumps(clean, ensure_ascii=False).encode("utf-8")
            if len(encoded) > 32000:
                clean = {"omitted": "Message exceeds the diagnostic size limit."}
                encoded = json.dumps(clean).encode()
            if size + len(encoded) > 180000:
                break
            selected.insert(0, clean)
            size += len(encoded)
        diagnostics = {
            "os": platform.system(),
            "python_version": platform.python_version(),
            "messages": selected,
            "total_messages": len(messages),
            "omitted_messages": len(messages) - len(selected),
        }
    report_id = str(uuid4())
    payload = {
        "schema_version": 1,
        "report_id": report_id,
        "description": description,
        "client_version": __version__,
        "diagnostics": diagnostics,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_REQUEST_BYTES:
        raise FeedbackReportError("Report is too large. Turn off diagnostics or save it locally.")
    return PreparedFeedback(report_id, _checked_endpoint(alysis_cloud.feedback_url()), body)


def send_feedback(prepared: PreparedFeedback) -> FeedbackReceipt:
    """Send exactly the approved bytes. Caller retains them for idempotent retries."""
    endpoint = _checked_endpoint(prepared.endpoint)
    key = load_persisted_profile_keys().get(alysis_cloud.PROFILE_KEY) or ""
    # Signed in: the report is tied to the account. Not signed in: it is sent without
    # credentials and the server admits it anonymously, rate-limited. Another
    # provider's key is never sent.
    signed_in = isinstance(key, str) and key.startswith("slk_")
    headers = {"Content-Type": "application/json"}
    if signed_in:
        headers["Authorization"] = f"Bearer {key}"
    try:
        # Never forward account credentials or the report across redirects.
        with httpx.Client(timeout=15.0, follow_redirects=False) as client:
            with client.stream(
                "POST",
                endpoint,
                content=prepared.body,
                headers=headers,
            ) as response:
                if response.status_code not in {200, 201}:
                    message = {
                        401: (
                            "Your Alysis sign-in expired. Run `alysis login`, then retry."
                            if signed_in
                            else "This server only accepts feedback from signed-in users. "
                            "Sign in with `alysis login`, or save locally."
                        ),
                        404: "Feedback delivery is not available on this server yet. Save locally or retry later.",
                        409: "This report ID has different content on the server. Go back and prepare a new report.",
                        413: "Report is too large. Go back and turn off diagnostics.",
                        429: "Feedback limit reached. Retry later or save locally.",
                    }.get(
                        response.status_code,
                        "Delivery could not be confirmed. Retry this report safely or save locally.",
                    )
                    raise FeedbackDeliveryError(
                        message, status_code=response.status_code, signed_in=signed_in
                    )
                chunks = bytearray()
                for chunk in response.iter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > 16384:
                        raise ValueError("Oversized receipt")
                data = json.loads(chunks)
                if (
                    not isinstance(data, dict)
                    or data.get("accepted") is not True
                    or data.get("report_id") != prepared.report_id
                    or not isinstance(data.get("created_at"), str)
                ):
                    raise ValueError("Invalid receipt")
                datetime.fromisoformat(data["created_at"].replace("Z", "+00:00"))
                return FeedbackReceipt(prepared.report_id, data["created_at"], not signed_in)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        # Do not display server bodies, proxy errors or request headers containing secrets.
        raise FeedbackReportError(
            "Delivery could not be confirmed. Retry this report safely; its ID prevents duplicates."
        ) from exc
